"""
Jarvis Always-On Voice Executive Harness (Pipecat Pipeline).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from collections.abc import Awaitable, Callable
from typing import Any
from loguru import logger

from jarvis.agent_watchers import AgentOrchestrator
from jarvis.config import settings
from jarvis.executive_filter import ExecutiveFilter
from jarvis.memory import NOT_THE_USER, create_memory_service
from jarvis.soul import load_soul
from jarvis.state import JarvisState


JARVIS_EXECUTIVE_SYSTEM_PROMPT = """You are Jarvis, a voice assistant that hands the user's work to agents: coding to T3 Code (or Claude Code or OpenCode when named), everything else (research, email, calendar, notes) to Hermes.

Core behavior:
1. Keep every response under 2 short, natural spoken sentences.
2. Never read out file paths, stack traces, diffs, or code blocks. Translate technical complexity into high-level decisions and trade-offs.
3. `send_agent_task` gives an agent new work. `answer_agent` answers the question or approval an agent is waiting on. `stop_agent` stops what an agent is doing.
4. You are also the user's friendly companion. Answer small talk ("how are you?", "good morning") warmly and briefly, like a colleague would; no tool calls for it.
5. Act, don't offer. When the user wants something done, or asks something you can't answer from general knowledge or this conversation (the state of their projects, their files, email, calendar or notes, anything current), call send_agent_task in this same reply for the agent whose role fits, and tell the user in one short line, like "I'll ask Hermes, one moment." Saying it without the tool call sends nothing. Never ask whether to send it, and never say you don't know when an agent could find out. Ask the user only when you can't tell what they want. You will be told when the agent finishes; then bring the answer to the user.
6. The user is not a native English speaker, and speech recognition often mishears names. When they correct or change a task an agent is still working on, send the whole corrected task with replace=true, and say in one short line that you stopped the old one.
"""

# A finished agent turn is long markdown; the conversation model retells it. Approvals never come this way.
# It goes in as a system message: agent output is not the user's voice and must not be able to drive a tool call.
AGENT_REPORT_PROMPT = (
    "[Not the user speaking: an agent finished its turn.] Tell the user what it did in at most two "
    "short spoken sentences. Do not call any tool; the user decides what happens next.\n\n"
)


def greeting_prompt(now: datetime, memories: list[str]) -> str:
    """What the brain gets at startup so Jarvis speaks first: the local time and a few things it remembers."""
    remembered = (
        " Things you remember about the user: " + "; ".join(memories) + ". Mention one only if it is timely"
        " (today or the next day or two), in one more sentence of at most eight words; otherwise mention none."
        if memories
        else ""
    )
    return (
        f"{NOT_THE_USER} Jarvis just started. It is {now:%A %H:%M}.] Greet the user first in at most five words that fit"
        f" the time of day, like \"Evening. I'm listening.\" Don't say the time or ask how they are.{remembered}"
        " Do not call any tool."
    )


def system_prompt(orchestrator: AgentOrchestrator, personality: str) -> str:
    """The conversation model's instructions: the rules, the user's SOUL.md personality, then the agents."""
    soul = (
        "\nThe user's own notes on your personality and how to talk to them. They set your tone only; the rules"
        " above always win, and your name stays Jarvis:\n" + personality + "\n"
        if personality
        else ""
    )
    return JARVIS_EXECUTIVE_SYSTEM_PROMPT + soul + agent_roster(orchestrator)


def agent_roster(orchestrator: AgentOrchestrator) -> str:
    """List the watched agents and what each is for, so the LLM routes by role and uses exact IDs."""
    if not orchestrator.adapters:
        return "\nNo coding agents are configured right now."
    default_id = orchestrator.default_agent_id
    lines = [
        f"- id `{a.config.id}`{' (DEFAULT for coding)' if a.config.id == default_id else ''}: {a.config.name}"
        + (f" — {a.config.role}" if a.config.role else "")
        for a in orchestrator.adapters.values()
    ]
    return (
        "\nWatched agents (use the exact id with the agent tools):\n"
        + "\n".join(lines)
        + f"\nRoute by role: a coding task goes to `{default_id}` unless the user names another agent; any other"
        " task that needs doing goes to the agent whose role covers it."
        " Small talk and general-knowledge questions, answer yourself; do not forward them."
        " When the user replies to a question an agent asked, use answer_agent, not send_agent_task. If"
        " answer_agent returns no_pending_question, the agent asked in its last reply: send the user's answer"
        " to that same agent with send_agent_task."
        + project_list(orchestrator)
    )


def project_list(orchestrator: AgentOrchestrator) -> str:
    """The user's project folders, for agents that can open one (T3 Code), so a spoken name maps to a folder."""
    from jarvis.t3_adapter import PROJECTS_LABEL, list_projects  # imported here: t3_adapter imports agent_watchers

    openers = [a.config.id for a in orchestrator.adapters.values() if hasattr(a, "open_project")]
    if not openers:
        return ""
    # shortcut: read once at startup; a repo cloned while Jarvis runs shows up after a restart.
    return (
        f"\n\nThe user's projects (folders under {PROJECTS_LABEL}). When the user wants to work on one (\"continue on the"
        f" Hugger Hustle platform\"), call send_agent_task for `{openers[0]}` with `project` set to the folder exactly"
        " as listed; speech recognition mishears names, so pick the closest one. Give a `task` only when the user"
        " says what to do there; \"continue on X\" or \"open X\" alone has no task. Coding with no project named"
        " needs no `project`.\n" + "\n".join(f"- {folder}" for folder in list_projects())
    )


def agent_tools() -> list:
    """The three agent tools the conversation model can call (also used by scripts/bench_routing.py)."""
    from pipecat.adapters.llm.schemas.tools_schema import FunctionSchema

    agent_id_property = {
        "type": "string",
        "description": (
            "Exact ID of the watched agent, from the list in your instructions. If omitted: a task goes to the"
            " default coding agent, an answer to the agent that asked, a stop to the agent that is working."
        ),
    }
    return [
        FunctionSchema(
            name="send_agent_task",
            description=(
                "Give a watched agent new work: a coding task, or any other task for the agent whose role covers it, "
                "or a follow-up to its last reply."
            ),
            properties={
                "agent_id": agent_id_property,
                "task": {
                    "type": "string",
                    "description": "The task, in clear words. With `project`, omit it when the user only opens the"
                    " project and names no work; never invent one.",
                },
                "project": {
                    "type": "string",
                    "description": "A folder from the user's project list (e.g. huggerhustle/platform-v2) when the"
                    " user names a project to work on. Omit otherwise.",
                },
                "replace": {
                    "type": "boolean",
                    "description": (
                        "True when the user corrects or changes a task this agent is still working on (a misheard"
                        " name, a changed detail, 'no, I meant ...'): the old task is stopped and this one runs"
                        " instead, so give the whole corrected task. False or omitted for an extra, separate task."
                    ),
                },
            },
            required=[],
        ),
        FunctionSchema(
            name="answer_agent",
            description=(
                "Answer the question or approval an agent is waiting on. "
                "Returns no_pending_question if it asked nothing."
            ),
            properties={
                "agent_id": agent_id_property,
                "answer": {"type": "string", "description": "The user's answer, in their words (e.g. 'yes', 'use Postgres')"},
            },
            required=["answer"],
        ),
        FunctionSchema(
            name="stop_agent",
            description="Stop what an agent is doing right now.",
            properties={"agent_id": agent_id_property},
            required=[],
        ),
    ]


def agent_tool_handlers(orchestrator: AgentOrchestrator) -> dict[str, Callable[[Any], Awaitable[None]]]:
    """Handlers for agent_tools(). Pipecat 1.x calls each with one FunctionCallParams (arguments, result_callback)."""

    def handler(action, ok_status: str, fail_status: str, find=orchestrator.find):
        async def handle(params: Any) -> None:
            args = params.arguments
            agent = find(args.get("agent_id"))
            if agent is None:
                await params.result_callback({"status": "agent_not_active", "agent_id": args.get("agent_id")})
                return
            ok = await action(agent, args)
            await params.result_callback({"status": ok_status if ok else fail_status, "agent_id": agent.config.id})

        return handle

    async def send(agent: Any, args: dict) -> bool:
        if args.get("replace") and agent.busy:
            await agent.interrupt()  # a correction: the old task must not come back as a second answer
        task, project = args.get("task", "").strip(), args.get("project")
        if not task and not project:
            return False  # nothing to send
        # Only T3 Code opens a project; the others work in their configured folder.
        ok = await (agent.open_project(project, task) if project and hasattr(agent, "open_project") else agent.send_task(task))
        if ok:
            orchestrator.remind_while_busy(agent)
        return ok

    return {
        "send_agent_task": handler(send, "sent", "failed"),
        # No id: an answer goes to the agent that asked, a stop to the one that is working.
        "answer_agent": handler(
            lambda a, args: a.answer(args.get("answer", "")), "answered", "no_pending_question", orchestrator.find_waiting
        ),
        "stop_agent": handler(lambda a, args: a.interrupt(), "stopped", "failed", orchestrator.find_busy),
    }


async def run_pipecat_harness() -> None:
    """Assemble and run the full Pipecat voice pipeline + background coding agent watchers."""
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.frames.frames import InterruptionFrame, LLMMessagesAppendFrame, OutputAudioRawFrame, TTSSpeakFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.expressive.policy import ExpressionGuidance, ExpressionPolicy
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMAssistantAggregatorParams,
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )
    from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
    from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

    from jarvis.speaker import SpeakerEmbedder, load_voiceprint
    from jarvis.voice_gates import EchoFilter, EchoReference, JarvisIntentGate, SpeakerVerifiedWhisperSTT

    # No voiceprint, no start: running unchecked would let any voice in the room give orders.
    try:
        voiceprint = load_voiceprint()
    except FileNotFoundError as exc:
        logger.error(str(exc))
        sys.exit(1)

    executive_filter = ExecutiveFilter()
    task_ref: dict[str, PipelineTask] = {}

    async def on_speak(text: str) -> None:
        if "task" in task_ref:
            # Record what Jarvis said, so the LLM knows which agent the user's next answer is about.
            await task_ref["task"].queue_frame(
                LLMMessagesAppendFrame(messages=[{"role": "assistant", "content": text}], run_llm=False)
            )
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
    orchestrator = AgentOrchestrator(state=state)
    orchestrator.load_from_file(settings.watch_config_path)

    # 1. Local mic + speaker (always on, no wake word); the echo filter keeps Jarvis's own voice out of the mic
    echo = EchoFilter()
    transport = LocalAudioTransport(
        LocalAudioTransportParams(audio_in_enabled=True, audio_out_enabled=True, audio_in_filter=echo)
    )

    # 2. Local Always-On STT (MLX Whisper on the Apple GPU), passing on only the owner's voice
    stt = SpeakerVerifiedWhisperSTT(
        embedder=SpeakerEmbedder(),
        voiceprint=voiceprint,
        threshold=settings.speaker_threshold,
        settings=SpeakerVerifiedWhisperSTT.Settings(model=settings.whisper_model),
    )

    # 3. Intent & Mute Gate ("Shut up" -> BEEP_ONLY, "You can talk" -> VOICE, Turn Classifier)
    intent_gate = JarvisIntentGate(state=state, executive_filter=executive_filter)

    # 4. Executive LLM Brain (OpenRouter)
    if settings.openrouter_api_key:
        from pipecat.services.openrouter.llm import OpenRouterLLMService

        llm = OpenRouterLLMService(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            settings=OpenRouterLLMService.Settings(
                model=settings.executive_model,
                # DeepSeek thinks by default, which more than doubles time to first word;
                # sort=latency routes to whichever provider currently answers fastest.
                extra={"extra_body": {"reasoning": {"enabled": False}, "provider": {"sort": "latency"}}},
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

    context = LLMContext(
        messages=[{"role": "system", "content": system_prompt(orchestrator, load_soul().personality)}],
        tools=tools,
    )
    # Pipecat 1.x runs voice detection in the user aggregator; it signals speech start/stop
    # upstream too, which is what tells Whisper when a sentence is complete.
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # A 0.6 s pause ends a Whisper sentence; shorter splits one sentence into two turns.
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.6, min_volume=0.5)),
            # Turns come only from JarvisIntentGate, after the voice and intent checks: raw room sound
            # would let background voices interrupt Jarvis and hold a turn open while they keep talking.
            user_turn_strategies=ExternalUserTurnStrategies(),
        ),
        # Jarvis runs all day: older talk is summarized (Pipecat defaults: 8000 tokens or 20 messages), so each
        # reply doesn't resend the whole day. It keeps the system prompt; offline there is no model to summarize.
        assistant_params=LLMAssistantAggregatorParams(enable_auto_context_summarization=llm is not None),
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

    processors.extend([transport.output(), EchoReference(echo), context_aggregator.assistant()])

    pipeline = Pipeline(processors)
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            # Pipecat teaches the LLM the voice's tags ([laughs], excited...) and keeps them out of the context.
            enable_expressive_conversation=True,
            expression_policy=ExpressionPolicy(guidance=[ExpressionGuidance.MATCH_STYLE]),
        ),
    )
    task_ref["task"] = task

    @task.event_handler("on_pipeline_started")
    async def greet(task: PipelineTask, frame) -> None:
        if llm is None:
            await on_speak("Jarvis here. I'm listening.")
            return
        memories: list[str] = []
        if memory is not None:
            try:
                memories = [m["memory"] for m in (await memory.get_memories())[-10:]]
            except Exception as exc:
                logger.warning(f"Could not read memories for the greeting ({exc!r}).")
        await task.queue_frame(
            # A user-role note: with no conversation yet, a trailing system message makes DeepSeek reply with junk.
            LLMMessagesAppendFrame(messages=[{"role": "user", "content": greeting_prompt(datetime.now(), memories)}], run_llm=True)
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


def main() -> None:
    try:
        asyncio.run(run_pipecat_harness())
    except KeyboardInterrupt:
        logger.info("Jarvis stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
