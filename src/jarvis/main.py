"""
Jarvis Always-On Voice Executive Harness (Pipecat Pipeline).
"""

from __future__ import annotations

import asyncio
import sys
from loguru import logger

from jarvis.agent_watchers import AgentOrchestrator
from jarvis.config import settings
from jarvis.executive_filter import ExecutiveFilter
from jarvis.state import JarvisState


JARVIS_EXECUTIVE_SYSTEM_PROMPT = """You are Jarvis, an executive voice harness orchestrating coding agents (Claude Code, OpenCode, Pi) for the user.

Core behavior:
1. Keep every response under 2 short, natural spoken sentences.
2. Never read out file paths, stack traces, diffs, or code blocks. Translate technical complexity into high-level decisions and trade-offs.
3. You have access to the `send_agent_decision` tool to pass the user's verbal instructions or approvals directly back to any watched coding agent.
"""


def agent_roster(orchestrator: AgentOrchestrator) -> str:
    """List the watched agents so the LLM knows them and uses their exact IDs in send_agent_decision."""
    if not orchestrator.adapters:
        return "\nNo coding agents are configured right now."
    lines = [f"- id `{a.config.id}`: {a.config.name}" for a in orchestrator.adapters.values()]
    return "\nWatched coding agents (use the exact id with send_agent_decision):\n" + "\n".join(lines)


async def run_pipecat_harness() -> None:
    """Assemble and run the full Pipecat voice pipeline + background coding agent watchers."""
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.frames.frames import InterruptionFrame, LLMMessagesAppendFrame, OutputAudioRawFrame, TTSSpeakFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.adapters.llm.schemas.tools_schema import FunctionSchema
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )
    from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
    from pipecat.turns.user_mute import AlwaysUserMuteStrategy

    from jarvis.voice_gates import JarvisIntentGate

    executive_filter = ExecutiveFilter()
    task_ref: dict[str, PipelineTask] = {}

    async def on_speak(text: str) -> None:
        if "task" in task_ref:
            # Record what Jarvis said, so the LLM knows which agent the user's next answer is about.
            await task_ref["task"].queue_frame(
                LLMMessagesAppendFrame(messages=[{"role": "assistant", "content": text}], run_llm=False)
            )
            await task_ref["task"].queue_frame(TTSSpeakFrame(text=text))

    async def on_beep(pcm_bytes: bytes) -> None:
        if "task" in task_ref:
            await task_ref["task"].queue_frame(
                OutputAudioRawFrame(audio=pcm_bytes, sample_rate=16000, num_channels=1)
            )

    async def on_interrupt() -> None:
        if "task" in task_ref:
            await task_ref["task"].queue_frame(InterruptionFrame())

    state = JarvisState(on_speak=on_speak, on_beep=on_beep, on_interrupt=on_interrupt)
    orchestrator = AgentOrchestrator(state=state, executive_filter=executive_filter)
    orchestrator.load_from_file(settings.watch_config_path)

    # 1. Local mic + speaker (always on, no wake word)
    transport = LocalAudioTransport(LocalAudioTransportParams(audio_in_enabled=True, audio_out_enabled=True))

    # 2. Local Always-On STT (MLX Whisper on the Apple GPU)
    from pipecat.services.whisper.stt import WhisperSTTServiceMLX

    stt = WhisperSTTServiceMLX(settings=WhisperSTTServiceMLX.Settings(model=settings.whisper_model))

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

    # Register tool so Jarvis can forward user decisions to Claude Code / OpenCode / Pi
    async def handle_send_agent_decision(
        function_name, tool_call_id, args, llm_service, context, result_callback
    ):
        agent_id = args.get("agent_id", "")
        instruction = args.get("instruction", "")
        ok = await orchestrator.route_user_decision(agent_id, instruction)
        await result_callback(
            {"status": "sent" if ok else "agent_not_active", "agent_id": agent_id}
        )

    tools = [
        FunctionSchema(
            name="send_agent_decision",
            description="Send the user's verbal decision, approval, or instruction to a watched coding agent.",
            properties={
                "agent_id": {
                    "type": "string",
                    "description": "Exact ID of the watched agent, from the list in your instructions",
                },
                "instruction": {
                    "type": "string",
                    "description": "Clear instruction or decision to send to the agent",
                },
            },
            required=["agent_id", "instruction"],
        )
    ]

    if llm is not None:
        llm.register_function("send_agent_decision", handle_send_agent_decision)

    context = LLMContext(
        messages=[{"role": "system", "content": JARVIS_EXECUTIVE_SYSTEM_PROMPT + agent_roster(orchestrator)}],
        tools=tools,
    )
    # Pipecat 1.x runs voice detection in the user aggregator; it signals speech start/stop
    # upstream too, which is what tells Whisper when a sentence is complete.
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # Smart Turn decides when a sentence is finished; 0.2 s is Pipecat's recommended VAD pause for it.
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2, min_volume=0.5)),
            # Through laptop speakers the mic hears Jarvis; ignore it while Jarvis talks.
            user_mute_strategies=[AlwaysUserMuteStrategy()] if settings.mute_mic_while_speaking else [],
        ),
    )

    # 5. Text-to-Speech (ElevenLabs if configured)
    processors = [
        transport.input(),
        stt,
        intent_gate,
        context_aggregator.user(),
    ]
    if llm is not None:
        processors.append(llm)

    if settings.elevenlabs_api_key:
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        tts = ElevenLabsTTSService(
            api_key=settings.elevenlabs_api_key,
            settings=ElevenLabsTTSService.Settings(voice=settings.elevenlabs_voice_id),
        )
        processors.append(tts)

    processors.extend([transport.output(), context_aggregator.assistant()])

    pipeline = Pipeline(processors)
    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))
    task_ref["task"] = task

    # Start background coding agent watchers (Claude Code, OpenCode, Pi)
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
