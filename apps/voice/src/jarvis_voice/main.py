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

from jarvis_core.soul import load_soul
from jarvis_voice import telemetry
from jarvis_voice.agent_adapter import WatchedAgentConfig
from jarvis_voice.agent_tools import agent_tool_handlers, agent_tools
from jarvis_voice.brain_client import BrainClientAdapter
from jarvis_voice.config import WHISPER_MODELS, settings
from jarvis_voice.executive_filter import ExecutiveFilter
from jarvis_voice.feedback import FeedbackLog
from jarvis_voice.memory import create_memory_service
from jarvis_voice.orchestrator import AgentOrchestrator
from jarvis_voice.pi_adapter import PiAdapter, install_extension
from jarvis_voice.prompts import AGENT_REPORT_PROMPT, LANGUAGES, NOT_THE_USER, greeting_prompt, system_prompt
from jarvis_voice.sessions import AgentSession, CodingSessionAdapter
from jarvis_voice.state import JarvisState
from jarvis_voice.system_prompts import load_overrides
from jarvis_voice.t3_adapter import current_thread, recent_threads


async def pick_connection(orchestrator: AgentOrchestrator, target: str) -> dict[str, Any]:
    """`jarvis connect [agent]`: pick the session every task goes to this run."""
    t3 = orchestrator.t3
    if t3 is not None and (target == "t3" or target == t3.config.id):
        return await pick_thread(orchestrator)
    brain = orchestrator.adapters.get("brain")
    if isinstance(brain, BrainClientAdapter) and target in ("t3", "brain", "claude", "opencode"):
        # The brain owns the T3 threads and the named clients, so its picker serves every one of them.
        brain.runner = "t3" if target in ("t3", "brain") else target
        return await pick_session(orchestrator, "brain")
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
    picked = _thread_for_this_folder(sessions)
    if picked is not None:
        session = sessions[picked]
        logger.info(f"[{adapter.config.id}] this folder's thread: {session.title} ({session.cwd})")
    else:
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


def _thread_for_this_folder(sessions: list[AgentSession]) -> int | None:
    """A T3 project script names its folder and nothing else: take the first thread there, no keyboard prompt."""
    root = os.environ.get("T3CODE_PROJECT_ROOT") or os.environ.get("T3CODE_WORKTREE_PATH")
    if not root:
        return None
    wanted = Path(root).expanduser().resolve()
    return next(
        (
            index
            for index, session in enumerate(sessions)
            if session.cwd and Path(session.cwd).expanduser().resolve() == wanted
        ),
        None,
    )


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


async def run_pipecat_harness(
    connect: str | None = None,
    *,
    meeting: bool = False,
    relay: Any = None,
    status: dict[str, Any] | None = None,
    hub_emit: Any = None,
) -> None:
    """Assemble and run the full Pipecat voice pipeline + background coding agent watchers.

    `relay` is the already-built `RemoteVoiceTransport` when the shared Jarvis server owns its routes; without it,
    one is built and it listens on its own (the voice without the brain). Either way a paired Mac's mic and speaker
    are always served, and take over from this Mac's while connected. `status`, when given, is kept up to date for
    the server's `/v1/status` answer. `hub_emit` is the brain's telemetry bus, so `jarvis top` can show each turn
    stage by stage; without it (standalone voice) those stages go nowhere.
    """
    telemetry.bind(hub_emit)
    live = status if status is not None else {}
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

    from jarvis_voice.cleanup import TranscriptCleaner
    from jarvis_voice.latency import latency_observer
    from jarvis_voice.mac_audio import MacVoiceTransport
    from jarvis_voice.speaker import VOICEPRINT_PATH, SpeakerEmbedder, SpeakerRegistry, load_voiceprint
    from jarvis_voice.voice_gates import (
        CLIPS_DIR,
        ActivationChime,
        JarvisIntentGate,
        SpeakerVerifiedWhisperSTT,
        last_assistant_text,
    )

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
    retell_prompt = overrides.get("retell") or AGENT_REPORT_PROMPT
    if not retell_prompt.startswith(NOT_THE_USER):
        # The marker keeps agent output out of the user's voice; an override may add rules, not drop it.
        retell_prompt = f"{NOT_THE_USER} an agent finished its turn.] {retell_prompt}"
    if cleaner is not None:
        logger.info(f"Cleaning up sentences with {cleaner.model}")
    task_ref: dict[str, PipelineTask] = {}

    async def on_speak(text: str) -> None:
        if not settings.elevenlabs_api_key:
            telemetry.emit("voice.tts", text=text, mode=state.mode.value, spoken=True)
        if "task" in task_ref:
            # TTSSpeakFrame saves the line to the context as spoken (cut short if interrupted),
            # so the LLM knows which agent the user's next answer is about.
            await task_ref["task"].queue_frame(TTSSpeakFrame(text=text))

    async def on_report(text: str) -> None:
        telemetry.emit("voice.retell", text=text, chars=len(text))
        if "task" in task_ref:
            held = (
                "The user paused explaining a bigger task to ask for this; give the answer briefly so they can"
                " continue, and do not start other work yet.\n\n"
                if state.working_context.active()
                else ""
            )
            await task_ref["task"].queue_frame(
                LLMMessagesAppendFrame(
                    messages=[{"role": "system", "content": retell_prompt + held + text}], run_llm=True
                )
            )

    async def on_beep(pcm_bytes: bytes) -> None:
        telemetry.emit("voice.tts", text="(beep)", mode=state.mode.value, beep=True)
        if "task" in task_ref:
            await task_ref["task"].queue_frame(
                OutputAudioRawFrame(audio=pcm_bytes, sample_rate=16000, num_channels=1)
            )

    async def on_interrupt() -> None:
        if "task" in task_ref:
            await task_ref["task"].queue_frame(InterruptionFrame())

    async def on_notice(text: str) -> None:
        """A mode note for the conversation model: no speech, and no new turn started."""
        if "task" in task_ref:
            await task_ref["task"].queue_frame(
                LLMMessagesAppendFrame(messages=[{"role": "system", "content": text}], run_llm=False)
            )

    state = JarvisState(
        on_speak=on_speak,
        on_beep=on_beep,
        on_interrupt=on_interrupt,
        on_report=on_report if settings.openrouter_api_key else None,  # no LLM offline: last line only
        on_notice=on_notice,
        meeting=meeting,  # from then on `state.meeting` follows the voice ("meeting mode on")
    )
    soul = load_soul()
    orchestrator = AgentOrchestrator(
        state=state, personality=soul.personality, waiting_prompt=overrides.get("waiting", "")
    )
    orchestrator.load_from_file(settings.watch_config_path)
    thread = await pick_connection(orchestrator, connect) if connect else None

    # 1. Mic and speaker (always on, no wake word) through macOS voice processing, which cancels Jarvis's own
    # voice so the user can talk over it on speakers. A paired Mac's mic and speaker are served on the same host and
    # port and own both while connected, so no mode has to be chosen up front; that canceller runs on that Mac instead,
    # where its speaker is. With no local audio hardware, Jarvis waits for a paired Mac.
    from jarvis_voice.remote_audio import make_transport

    relay_is_standalone = False
    if relay is None:
        relay = make_transport()
        try:
            await relay.start_server()
            relay_is_standalone = True
        except OSError as exc:
            logger.warning(
                f"The thin client's port is taken ({exc!r}); this Jarvis uses its own mic and speaker and serves no "
                "paired Mac."
            )
            relay = None
    transport: MacVoiceTransport = MacVoiceTransport(
        TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        meeting=meeting,
        hub=relay.hub if relay is not None else None,
        ear=relay.ear if relay is not None else None,
    )
    # "Meeting mode on" by voice starts hearing the call and speaking into it, without a restart.
    state.on_meeting = transport.set_meeting

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
        # Its silence limit adds to the VAD's 0.45 s below: ~2.45 s of silence ends a sentence held mid-thought.
        turn_analyzer=LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=2.0)) if settings.smart_turn else None,
        settings=SpeakerVerifiedWhisperSTT.Settings(model=settings.whisper_model, language=Language(settings.language)),
    )

    # 3. Executive LLM Brain (OpenRouter)
    if settings.openrouter_api_key:
        from jarvis_voice.llm import JarvisLLM

        llm = JarvisLLM(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            working_context=state.working_context,
            settings=JarvisLLM.Settings(
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
        from jarvis_voice.llm import traced_tool

        for name, handler in agent_tool_handlers(orchestrator).items():
            llm.register_function(name, traced_tool(name, handler))

    context = LLMContext(tools=tools)
    # Pipecat 1.x runs voice detection in the user aggregator; it signals speech start/stop
    # upstream too, which is what tells Whisper when a sentence is complete.
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # A pause ends a Whisper sentence: 0.45 s so a brief breath stays one segment, then Smart Turn decides;
            # without Smart Turn, 0.7 s.
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.45 if settings.smart_turn else 0.7, min_volume=0.5)),
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
        processors.extend([tts, ActivationChime()])

    assistant_agg = context_aggregator.assistant()

    @assistant_agg.event_handler("on_assistant_turn_stopped")
    async def _on_spoken(_agg: Any, message: Any) -> None:
        if getattr(message, "content", ""):
            telemetry.emit(
                "voice.tts",
                text=message.content,
                mode=state.mode.value,
                interrupted=bool(getattr(message, "interrupted", False)),
            )

    processors.extend([transport.output(), assistant_agg])

    pipeline = Pipeline(processors)
    task = PipelineTask(
        pipeline,
        # Jarvis listens 24/7; Pipecat's 5-minute call idle timeout would cancel the pipeline and close the mic.
        idle_timeout_secs=None,
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
        if not transport.has_local_audio:
            # No mic or speaker on this Mac (a headless Mac mini): a greeting spoken now would play to an empty room.
            task.create_task(greet_when_connected())
            return
        await greet_now()

    async def greet_when_connected() -> None:
        if relay is not None:
            await relay.wait_for_client()  # the paired Mac may connect well after this Mac is ready
        await greet_now()

    async def greet_now() -> None:
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
    live["local_audio"] = transport.has_local_audio

    def emit_ear() -> None:
        telemetry.emit(
            "voice.ear",
            local_audio=transport.has_local_audio,
            relay=relay is not None and relay.hub.client is not None,
            language=settings.language,
            whisper=settings.whisper_model,
            master_check=settings.master_check,
            executive_model=settings.executive_model,
            meeting=state.meeting,
        )

    if relay is not None:
        relay.hub.on_attach.append(emit_ear)
        relay.hub.on_detach.append(emit_ear)
    emit_ear()
    telemetry.emit("voice.mode", mode=state.mode.value, meeting=state.meeting)
    await orchestrator.start_all()
    live["state"] = "listening"
    logger.info("Jarvis is listening (no wake word required). Say 'Shut up' to mute to beep-only mode.")

    runner = PipelineRunner()
    try:
        await runner.run(task)
    finally:
        live["state"] = "stopped"
        await orchestrator.stop_all()
        await executive_filter.aclose()
        if cleaner is not None:
            await cleaner.aclose()
        if relay is not None and relay_is_standalone:
            await relay.stop_server()
