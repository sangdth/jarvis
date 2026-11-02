from __future__ import annotations

import struct
from pathlib import Path
from loguru import logger

from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    InputAudioRawFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from jarvis.config import settings
from jarvis.executive_filter import ExecutiveFilter
from jarvis.state import JarvisMode, JarvisState, UserIntent


class SpeakerVerificationGate(FrameProcessor):
    """
    Pipecat FrameProcessor that enforces 'talk to me only'.
    Scores incoming microphone audio frames against the enrolled owner's voiceprint
    using Picovoice Eagle. Drops audio from other speakers, TV, or background voices.
    """

    def __init__(
        self,
        access_key: str | None = None,
        voiceprint_path: Path | None = None,
        threshold: float | None = None,
    ) -> None:
        super().__init__()
        self.access_key = access_key if access_key is not None else settings.picovoice_access_key
        self.voiceprint_path = voiceprint_path or settings.resolved_voiceprint_path
        self.threshold = threshold if threshold is not None else settings.speaker_similarity_threshold

        self._eagle = None
        self._pcm_buffer: list[int] = []
        self._is_owner_speaking: bool = True  # Default pass-through if Eagle isn't configured yet
        self._init_eagle()

    def _init_eagle(self) -> None:
        if not self.access_key or not self.voiceprint_path.exists():
            logger.warning(
                "Picovoice Eagle voiceprint or key not configured yet "
                f"({self.voiceprint_path}). Running in pass-through speaker mode. "
                "Run `jarvis-enroll` to lock Jarvis to your voice only."
            )
            return

        try:
            import pveagle

            voiceprint_bytes = self.voiceprint_path.read_bytes()
            profile = pveagle.EagleProfile.from_bytes(voiceprint_bytes)
            self._eagle = pveagle.create_recognizer(
                access_key=self.access_key,
                speaker_profiles=[profile],
            )
            logger.info(
                f"Loaded owner voiceprint from {self.voiceprint_path} "
                f"(threshold={self.threshold:.2f})"
            )
        except Exception as exc:
            logger.error(f"Failed to initialize Picovoice Eagle recognizer: {exc}")
            self._eagle = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if self._eagle is None:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, UserStartedSpeakingFrame):
            self._pcm_buffer.clear()
            self._is_owner_speaking = False
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, UserStoppedSpeakingFrame):
            self._pcm_buffer.clear()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, InputAudioRawFrame):
            num_samples = len(frame.audio) // 2
            samples = struct.unpack(f"<{num_samples}h", frame.audio)
            self._pcm_buffer.extend(samples)

            frame_len = self._eagle.frame_length
            while len(self._pcm_buffer) >= frame_len:
                chunk = self._pcm_buffer[:frame_len]
                del self._pcm_buffer[:frame_len]
                scores = self._eagle.process(chunk)
                if scores and scores[0] >= self.threshold:
                    self._is_owner_speaking = True

            # Only forward audio to STT once the speaker score crosses the owner threshold
            if self._is_owner_speaking:
                await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)


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
