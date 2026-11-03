from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import numpy as np
from loguru import logger

import soxr
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.frames.frames import (
    FilterControlFrame,
    Frame,
    OutputAudioRawFrame,
    ProposedUserStartedSpeakingFrame,
    ProposedUserStoppedSpeakingFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.whisper.stt import WhisperSTTServiceMLX
from voiceclean.aec import AEC

MIC_RATE = 16000


class EchoFilter(BaseAudioFilter):
    """Takes Jarvis's own voice out of the mic (voiceclean AEC), so it doesn't hear itself through speakers."""

    def __init__(self) -> None:
        self.aec = AEC(sample_rate=MIC_RATE)

    async def start(self, sample_rate: int) -> None:
        if sample_rate != MIC_RATE:
            raise ValueError(f"EchoFilter runs at {MIC_RATE} Hz, the mic gives {sample_rate}")

    async def stop(self) -> None:
        pass

    async def process_frame(self, frame: FilterControlFrame) -> None:
        pass

    async def filter(self, audio: bytes) -> bytes:
        return self.aec.process(audio)  # b"" while it gathers a 40 ms chunk; Pipecat skips empty frames


class EchoReference(FrameProcessor):
    """
    Feeds what Jarvis plays to the EchoFilter. Sits after transport.output(), which passes a frame on only
    once it has gone to the speaker, so the reference lines up with what the mic hears.
    """

    def __init__(self, echo: EchoFilter) -> None:
        super().__init__()
        self._aec = echo.aec
        self._resampler: soxr.ResampleStream | None = None
        self._rate = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, OutputAudioRawFrame):
            self._aec.feed_reference(self.to_mic_rate(frame.audio, frame.sample_rate))
        await self.push_frame(frame, direction)

    def to_mic_rate(self, audio: bytes, rate: int) -> bytes:
        """TTS plays at 24 kHz, beeps at 16 kHz: one streaming resampler per rate, so frame edges don't click."""
        if rate == MIC_RATE:
            return audio
        if rate != self._rate:
            self._resampler, self._rate = soxr.ResampleStream(rate, MIC_RATE, 1, dtype="int16"), rate
        return self._resampler.resample_chunk(np.frombuffer(audio, dtype=np.int16)).tobytes()

from jarvis.executive_filter import ExecutiveFilter
from jarvis.speaker import SpeakerEmbedder, cosine
from jarvis.state import JarvisMode, JarvisState, UserIntent, classify_fast_command


class SpeakerVerifiedWhisperSTT(WhisperSTTServiceMLX):
    """
    MLX Whisper that only passes on the owner's sentences.
    Pipecat hands run_stt one whole VAD sentence; the voice score runs alongside Whisper, so it adds no delay.
    Mute / unmute phrases pass from any voice: the worst case is that someone else silences Jarvis.
    """

    def __init__(self, *, embedder: SpeakerEmbedder, voiceprint: np.ndarray, threshold: float, **kwargs) -> None:
        super().__init__(**kwargs)
        self._embedder = embedder
        self._voiceprint = voiceprint
        self._threshold = threshold

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        score_task = asyncio.create_task(asyncio.to_thread(self._score, audio))
        frames = [frame async for frame in super().run_stt(audio)]
        score = await score_task
        for frame in frames:
            if isinstance(frame, TranscriptionFrame):
                owner = score >= self._threshold
                passed = owner or classify_fast_command(frame.text) is not None
                logger.info(f"[Speaker] score={score:.2f} {'pass' if passed else 'drop'}: {frame.text!r}")
                if not passed:
                    continue
            yield frame

    def _score(self, audio: bytes) -> float:
        return cosine(self._embedder.embed(audio), self._voiceprint)


class JarvisIntentGate(FrameProcessor):
    """
    Pipecat FrameProcessor placed right after local STT (MLX Whisper).
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

        # While muted only "you can talk" matters; skip the paid LLM call for everything else.
        if self.state.mode == JarvisMode.BEEP_ONLY and classify_fast_command(transcript) is None:
            return

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

        # User is addressing Jarvis: this sentence is the whole user turn. The aggregator runs on
        # ExternalUserTurnStrategies, so only these frames start a turn (and interrupt Jarvis).
        await self.push_frame(ProposedUserStartedSpeakingFrame(), direction)
        await self.push_frame(frame, direction)
        await self.push_frame(ProposedUserStoppedSpeakingFrame(), direction)
