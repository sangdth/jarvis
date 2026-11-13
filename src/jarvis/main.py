"""
Jarvis Always-On Voice Executive Harness (Pipecat Pipeline).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from jarvis.agent_adapter import WatchedAgentConfig
from jarvis.agent_tools import agent_tool_handlers, agent_tools
from jarvis.config import WHISPER_MODELS, settings
from jarvis.executive_filter import ExecutiveFilter
from jarvis.feedback import FeedbackLog
from jarvis.memory import create_memory_service
from jarvis.orchestrator import AgentOrchestrator
from jarvis.pi_adapter import PiAdapter, install_extension
from jarvis.prompts import AGENT_REPORT_PROMPT, LANGUAGES, greeting_prompt, system_prompt
from jarvis.sessions import CodingSessionAdapter
from jarvis.soul import load_soul
from jarvis.state import JarvisState
from jarvis.system_prompts import load_overrides
from jarvis.t3_adapter import current_thread, recent_threads


async def pick_connection(orchestrator: AgentOrchestrator, target: str) -> dict[str, Any]:
    """`jarvis connect [agent]`: pick the T3 thread or the coding session that every task goes to this run."""
    t3 = orchestrator.t3
    if target == "t3" or (t3 is not None and target == t3.config.id):
        return await pick_thread(orchestrator)
    return await pick_session(orchestrator, target)


async def pick_thread(orchestrator: AgentOrchestrator) -> dict[str, Any]:
    """`jarvis connect`: attach T3 Code to the thread this runs in (a T3 project script), else to one picked here."""
    t3 = orchestrator.t3
    if t3 is None:
        sys.exit("jarvis connect needs a T3 Code agent enabled in agents.json.")
    try:
        snapshot = await t3.shell_snapshot()
    except Exception as exc:
        sys.exit(f"Could not read T3 threads; is the T3 app running? ({exc!r})")
    root = os.environ.get("T3CODE_PROJECT_ROOT")  # set only in a T3 project script
    thread = current_thread(snapshot, root, os.environ.get("T3CODE_WORKTREE_PATH")) if root else None
    if not thread:
        threads = recent_threads(snapshot)
        if not threads:
            sys.exit("No T3 threads to connect to.")
        labels = [f"{t['project']}: {t['title']}  ({t['status']}, {_when(t['updatedAt'])})" for t in threads]
        thread = threads[choose("Your recent T3 Code threads", labels, "Thread to continue")]
    # Its waiting question, if any, is read out once T3 Code connects.
    t3.attach(thread["id"], thread["root"], connected=True, title=thread["title"])
    logger.info(f"Connected to T3 thread {thread['id']} ({thread['title']}, {thread['root']})")
    return {**thread, "agent_id": t3.config.id, "agent_name": t3.config.name}


async def pick_session(orchestrator: AgentOrchestrator, agent_id: str) -> dict[str, Any]:
    """`jarvis connect claude|opencode|pi`: continue one of that client's sessions, picked at the keyboard."""
    adapter = orchestrator.adapters.get(agent_id)
    if agent_id == "pi" and adapter is None:  # Pi is reached only through an open window, so it is not in agents.json
        if install_extension():
            sys.exit("Installed Jarvis's Pi extension. Run /reload in your open Pi windows, then run this again.")
        adapter = PiAdapter(WatchedAgentConfig(id="pi", name="Pi", type="pi"), orchestrator.state)
    if not isinstance(adapter, CodingSessionAdapter):
        ids = [a.config.id for a in orchestrator.adapters.values() if isinstance(a, CodingSessionAdapter)] + ["pi"]
        sys.exit(f"jarvis connect {agent_id}: not a coding agent enabled in agents.json; use one of {ids}.")
    name = adapter.config.name
    try:
        sessions = await adapter.list_sessions()
    except Exception as exc:
        sys.exit(f"Could not read {name} sessions; is {name} running? ({exc!r})")
    if not sessions:
        sys.exit("No Pi window is open with Jarvis's extension. Open pi in the project, then run this again."
                 if agent_id == "pi" else f"No {name} sessions to continue.")
    labels = [f"{Path(s.cwd).name}: {s.title}  ({_when(s.updated_at)})" for s in sessions]
    session = sessions[choose(f"Your recent {name} sessions", labels, "Session to continue")]
    if not await adapter.pick_session(session, connected=True):
        sys.exit(f"Could not connect to that {name} session; it is busy or gone. Try again.")
    orchestrator.adapters[adapter.config.id] = adapter
    logger.info(f"Connected to {name} session {session.id} ({session.title}, {session.cwd})")
    return {"id": session.id, "title": session.title, "project": Path(session.cwd).name,
            "agent_id": adapter.config.id, "agent_name": name}


def _when(moment: str | float | None) -> str:
    """A thread's ISO time or a session's epoch seconds, as "Nov 08 13:02" in local time."""
    if not moment:
        return ""
    when = datetime.fromisoformat(moment) if isinstance(moment, str) else datetime.fromtimestamp(moment)
    return when.astimezone().strftime("%b %d %H:%M")


def choose(heading: str, labels: list[str], question: str) -> int:
    """Ask at the keyboard which numbered row to use; returns its index."""
    print(f"\n=== {heading} ===\n")
    for n, label in enumerate(labels, 1):
        print(f"{n:>2}. {label}")
    choice = ""
    while not (choice.isdigit() and 1 <= int(choice) <= len(labels)):
        try:  # blocking is fine: nothing else runs before the pipeline starts
            choice = input(f"\n{question} (1-{len(labels)}): ").strip()
        except EOFError:
            sys.exit(0)
    return int(choice) - 1


async def run_pipecat_harness(connect: str | None = None, *, meeting: bool = False) -> None:
    """Assemble and run the full Pipecat voice pipeline + background coding agent watchers."""
    from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.expressive.policy import ExpressionGuidance, ExpressionPolicy
    from pipecat.frames.frames import InterruptionFrame, LLMMessagesAppendFrame, OutputAudioRawFrame, TTSSpeakFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMAssistantAggregatorParams,
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )
    from pipecat.transcriptions.language import Language
    from pipecat.transports.base_transport import TransportParams
    from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

    from jarvis.cleanup import TranscriptCleaner
    from jarvis.latency import latency_observer
    from jarvis.mac_audio import MacVoiceTransport
    from jarvis.speaker import VOICEPRINT_PATH, SpeakerEmbedder, SpeakerRegistry, load_voiceprint
    from jarvis.voice_gates import CLIPS_DIR, JarvisIntentGate, SpeakerVerifiedWhisperSTT, last_assistant_text

    voiceprint = None
    if settings.master_check:
        # Asked for the master check but no voiceprint: refuse rather than let any voice give orders unannounced.
        try:
            voiceprint = load_voiceprint()
        except FileNotFoundError as exc:
            logger.error(f"MASTER_CHECK is on: {exc}")
            sys.exit(1)
        check = f"master check on (threshold {settings.speaker_threshold}, voiceprint {VOICEPRINT_PATH})"
    else:
        check = "master check off: any voice may talk to Jarvis"
        if VOICEPRINT_PATH.exists():
            logger.warning(f"{VOICEPRINT_PATH} is there but unused; set MASTER_CHECK=true in .env to use it.")
    if meeting:
        guests = settings.meeting_guests if settings.master_check else "allow (everyone counts as the master)"
        check += f"; meeting guests: {guests}"
    # The log is where misses get investigated; it has to say which setup heard them.
    logger.info(f"Hearing {settings.language} with Whisper {settings.whisper_model}, {check}")

    if settings.save_segments > 0:
        logger.warning(f"SAVE_SEGMENTS={settings.save_segments}: every sentence heard is saved to {CLIPS_DIR}.")
    if settings.language != "en" and settings.whisper_model == WHISPER_MODELS["en"]:
        logger.warning(f"WHISPER_MODEL pins the English model; delete it from .env to hear {settings.language} better.")
    executive_filter = ExecutiveFilter(feedback=FeedbackLog())  # logs decisions and corrections for retraining
    overrides = load_overrides()
    if overrides:
        logger.info(f"SYSTEM.md overrides: {', '.join(name.capitalize() for name in overrides)}")
    use_cleanup = settings.transcript_cleanup and settings.openrouter_api_key
    cleaner = TranscriptCleaner(prompt=overrides.get("cleanup")) if use_cleanup else None
    if cleaner is not None:
        logger.info(f"Cleaning up sentences with {cleaner.model}")
    task_ref: dict[str, PipelineTask] = {}

    async def on_speak(text: str) -> None:
        if "task" in task_ref:
            # TTSSpeakFrame saves the line to the context as spoken (cut short if interrupted),
            # so the LLM knows which agent the user's next answer is about.
            await task_ref["task"].queue_frame(TTSSpeakFrame(text=text))

    async def on_report(text: str) -> None:
        if "task" in task_ref:
            await task_ref["task"].queue_frame(
                LLMMessagesAppendFrame(
                    messages=[{"role": "system", "content": AGENT_REPORT_PROMPT + text}], run_llm=True
                )
            )

    async def on_beep(pcm_bytes: bytes) -> None:
        if "task" in task_ref:
            await task_ref["task"].queue_frame(
                OutputAudioRawFrame(audio=pcm_bytes, sample_rate=16000, num_channels=1)
            )

    async def on_interrupt() -> None:
        if "task" in task_ref:
            await task_ref["task"].queue_frame(InterruptionFrame())

    state = JarvisState(
        on_speak=on_speak,
        on_beep=on_beep,
        on_interrupt=on_interrupt,
        on_report=on_report if settings.openrouter_api_key else None,  # no LLM offline: last line only
    )
    soul = load_soul()
    orchestrator = AgentOrchestrator(state=state, personality=soul.personality)
    orchestrator.load_from_file(settings.watch_config_path)
    thread = await pick_connection(orchestrator, connect) if connect else None

    # 1. Local mic + speaker (always on, no wake word) through macOS voice processing, which cancels Jarvis's own
    # voice so the user can talk over it on speakers
    transport = MacVoiceTransport(TransportParams(audio_in_enabled=True, audio_out_enabled=True), meeting=meeting)

    # 2. Local Always-On STT (MLX Whisper on the Apple GPU); with the master check, passing on only the master's voice,
    # known enrolled speakers, or a new speaker being enrolled (or tagging room in meetings).
    registry = SpeakerRegistry() if voiceprint is not None else SpeakerRegistry(folder=None)
    stt = SpeakerVerifiedWhisperSTT(
        embedder=SpeakerEmbedder() if voiceprint is not None else None,
        voiceprint=voiceprint,
        threshold=settings.speaker_threshold,
        meeting=meeting,
        registry=registry,
        state=state,
        # Its silence limit adds to the VAD's 0.2 s below: 1.5 s of silence ends a sentence held mid-thought.
        turn_analyzer=LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=1.3)) if settings.smart_turn else None,
        settings=SpeakerVerifiedWhisperSTT.Settings(model=settings.whisper_model, language=Language(settings.language)),
    )

    # 3. Executive LLM Brain (OpenRouter)
    if settings.openrouter_api_key:
        from pipecat.services.openrouter.llm import OpenRouterLLMService

        llm = OpenRouterLLMService(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            settings=OpenRouterLLMService.Settings(
                model=settings.executive_model,
                system_instruction=system_prompt(
                    orchestrator,
                    soul.personality,
                    thread,
                    settings.language,
                    meeting=meeting,
                    guests=settings.master_check,
                ),
                # Thinking stays on for better replies, at a slower first word; sort=latency routes to whichever
                # provider currently answers fastest.
                extra={
                    "extra_body": {
                        "reasoning": {"enabled": True},
                        "provider": {"sort": "latency"},
                    }
                },
            ),
            default_headers={
                "HTTP-Referer": "https://github.com/sangdth/jarvis",
                "X-Title": "Jarvis",
            },
        )
        logger.info(f"Initialized OpenRouter LLM: {settings.executive_model}")
    else:
        logger.warning("No OPENROUTER_API_KEY configured; running in offline mode.")
        llm = None

    tools = agent_tools()

    if llm is not None:
        for name, handler in agent_tool_handlers(orchestrator).items():
            llm.register_function(name, handler)

    context = LLMContext(tools=tools)
    # Pipecat 1.x runs voice detection in the user aggregator; it signals speech start/stop
    # upstream too, which is what tells Whisper when a sentence is complete.
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # A pause ends a Whisper sentence: 0.2 s, then Smart Turn decides; without it 0.6 s, as shorter splits
            # one sentence into two turns.
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2 if settings.smart_turn else 0.6, min_volume=0.5)),
            # Turns come only from JarvisIntentGate, after the voice and intent checks: raw room sound
            # would let background voices interrupt Jarvis and hold a turn open while they keep talking.
            user_turn_strategies=ExternalUserTurnStrategies(),
        ),
        # Jarvis runs all day: older talk is summarized (Pipecat defaults: 8000 tokens or 20 messages), so each
        # reply doesn't resend the whole day. The system prompt (system_instruction) sits outside the context, so
        # summaries never touch it; offline there is no model to summarize.
        assistant_params=LLMAssistantAggregatorParams(enable_auto_context_summarization=llm is not None),
    )

    # 4. Intent & Mute Gate ("Shut up" -> BEEP_ONLY, "You can talk" -> VOICE, Turn Classifier); Jarvis's last reply
    # lets it hear a short or garbled answer to Jarvis's own question.
    intent_gate = JarvisIntentGate(
        state=state,
        executive_filter=executive_filter,
        last_said=lambda: last_assistant_text(context.messages),
        meeting=meeting,
        registry=registry,
        cleaner=cleaner,
    )

    # 5. Text-to-Speech (ElevenLabs if configured)
    processors = [
        transport.input(),
        stt,
        intent_gate,
        context_aggregator.user(),
    ]
    # Long-term memory sits right before the brain: it adds what Jarvis remembers to each request (~22 ms).
    memory = create_memory_service() if llm is not None else None
    if memory is not None:
        processors.append(memory)
    if llm is not None:
        processors.append(llm)

    if settings.elevenlabs_api_key:
        # Text-to-Dialogue serves eleven_v4_turbo (its default): ~100 ms to first audio and real laughs and sighs;
        # the plain TTS socket rejects that model.
        from pipecat.services.elevenlabs.dialogue.tts import ElevenLabsDialogueTTSService

        tts = ElevenLabsDialogueTTSService(
            api_key=settings.elevenlabs_api_key,
            settings=ElevenLabsDialogueTTSService.Settings(voice=settings.elevenlabs_voice_id),
        )
        processors.append(tts)

    processors.extend([transport.output(), context_aggregator.assistant()])

    pipeline = Pipeline(processors)
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            # Services report their own times, so each `[Latency]` line names Whisper, the LLM and the voice.
            enable_metrics=True,
            # Pipecat teaches the LLM the voice's tags ([laughs], excited...) and keeps them out of the context.
            enable_expressive_conversation=True,
            expression_policy=ExpressionPolicy(guidance=[ExpressionGuidance.MATCH_STYLE]),
        ),
        observers=[latency_observer()],
    )
    task_ref["task"] = task

    @task.event_handler("on_pipeline_started")
    async def greet(task: PipelineTask, frame) -> None:
        if thread:  # back from the keyboard mid-work: say where we are, skip the small talk
            await on_speak(LANGUAGES[settings.language]["connected"].format(title=thread["title"], project=thread["project"]))
            return
        if meeting:  # never read out personal memories in front of a meeting room
            await on_speak(LANGUAGES[settings.language]["meeting"])
            return
        if llm is None:
            await on_speak("Jarvis here. I'm listening.")
            return
        memories: list[str] = []
        if memory is not None:
            try:
                query = f"{datetime.now():%A %B %d} today tomorrow schedule upcoming"
                memories = [m["memory"] for m in (await memory._retrieve_memories(query))[:10]]
            except Exception as exc:
                logger.warning(f"Could not read memories for the greeting ({exc!r}).")
        await task.queue_frame(
            # A user-role note: with no conversation yet, a trailing system message makes DeepSeek reply with junk.
            LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": greeting_prompt(datetime.now(), memories, settings.language)}], run_llm=True
            )
        )

    # Start background coding agent watchers (Claude Code, OpenCode)
    await orchestrator.start_all()
    logger.info("Jarvis is listening (no wake word required). Say 'Shut up' to mute to beep-only mode.")

    runner = PipelineRunner()
    try:
        await runner.run(task)
    finally:
        await orchestrator.stop_all()
        await executive_filter.aclose()
        if cleaner is not None:
            await cleaner.aclose()
