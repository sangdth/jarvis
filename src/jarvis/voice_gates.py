from __future__ import annotations

from loguru import logger

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from jarvis.executive_filter import ExecutiveFilter
from jarvis.state import JarvisMode, JarvisState, UserIntent


class JarvisIntentGate(FrameProcessor):
    """
    Pipecat FrameProcessor placed right after local STT (Faster-Whisper).
    1. Immediately catches 'Shut up' / 'Be quiet' -> switches Jarvis to BEEP_ONLY mode.
    2. Immediately catches 'You can talk' -> restores VOICE mode and reads queued topics.
    3. When in BEEP_ONLY mode, suppresses all normal conversational responses.
    4. When in VOICE mode, runs the Always-On Turn Classifier so Jarvis only responds
       when actually needed (no wake word required).
    """

    def __init__(self, state: JarvisState, executive_filter: ExecutiveFilter) -> None:
        super().__init__()
        self.state = state
        self.executive_filter = executive_filter

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if not isinstance(frame, TranscriptionFrame):
            await self.push_frame(frame, direction)
            return

        transcript = frame.text.strip()
        if not transcript:
            return

        logger.info(f"[User Transcript] ({self.state.mode.value}): {transcript!r}")

        recent_topic = next(
            (t.summary for t in reversed(list(self.state.active_agent_context.values()))),
            None,
        )
        intent = await self.executive_filter.classify_user_utterance(
            transcript=transcript,
            recent_jarvis_question=recent_topic,
        )

        if intent == UserIntent.MUTE:
            logger.warning("User requested silence ('Shut up' / 'Be quiet'). Entering BEEP_ONLY mode.")
            await self.state.set_mute()
            return

        if intent == UserIntent.UNMUTE:
            logger.info("User restored voice mode ('You can talk').")
            await self.state.set_unmute()
            return

        if self.state.mode == JarvisMode.BEEP_ONLY:
            logger.debug("Jarvis is in BEEP_ONLY mode; ignoring non-unmute speech.")
            return

        if intent == UserIntent.IGNORE_BACKGROUND:
            logger.debug(f"Ignored self-talk / unaddressed speech: {transcript!r}")
            return

        # User is addressing Jarvis while in VOICE mode — push transcription downstream to Executive LLM
        await self.push_frame(frame, direction)
