from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace

import numpy as np
from loguru import logger
from pipecat.frames.frames import (
    Frame,
    ProposedUserStartedSpeakingFrame,
    ProposedUserStoppedSpeakingFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.whisper.stt import WhisperSTTServiceMLX

from jarvis.executive_filter import ExecutiveFilter
from jarvis.speaker import SpeakerEmbedder, cosine
from jarvis.state import JarvisMode, JarvisState, UserIntent, classify_fast_command

# Whisper ends a sentence that trails off mid-thought with one of these ("Open the...").
TRAILING_OFF = ("...", "…", "-", "—")
# How long a trailing-off sentence waits for the rest, counted from the last stretch of silence.
HOLD_SECS = 2.0


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
        # A sentence that trailed off ("Open the..."), waiting for the rest after a pause.
        self._held: TranscriptionFrame | None = None
        self._hold_timer: asyncio.Task | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if not isinstance(frame, TranscriptionFrame):
            if self._held is not None:
                # The user speaks again: wait for that sentence; once they stop, give Whisper the window again.
                if isinstance(frame, VADUserStartedSpeakingFrame):
                    self._cancel_hold_timer()
                elif isinstance(frame, VADUserStoppedSpeakingFrame):
                    self._start_hold_timer(direction)
            await self.push_frame(frame, direction)
            return

        transcript = frame.text.strip()
        if not transcript:
            return

        # Mute / unmute act at once and never join a held sentence.
        if classify_fast_command(transcript) is None:
            if self._held is not None:
                self._cancel_hold_timer()
                frame = replace(frame, text=f"{self._held.text.strip()} {transcript}")
                self._held = None
            if frame.text.strip().endswith(TRAILING_OFF):
                logger.info(f"[User Transcript] holding for the rest: {frame.text.strip()!r}")
                self._held = frame
                self._start_hold_timer(direction)
                return

        await self._gate(frame, direction)

    async def cleanup(self) -> None:
        self._cancel_hold_timer()
        await super().cleanup()

    def _start_hold_timer(self, direction: FrameDirection) -> None:
        self._cancel_hold_timer()
        self._hold_timer = asyncio.create_task(self._release_held(direction))

    def _cancel_hold_timer(self) -> None:
        if self._hold_timer is not None:
            self._hold_timer.cancel()
            self._hold_timer = None

    async def _release_held(self, direction: FrameDirection) -> None:
        """Nothing more came: the held sentence goes on alone, so a real "Hmm..." still gets an answer."""
        await asyncio.sleep(HOLD_SECS)
        frame, self._held, self._hold_timer = self._held, None, None
        if frame is not None:
            await self._gate(frame, direction)

    async def _gate(self, frame: TranscriptionFrame, direction: FrameDirection) -> None:
        transcript = frame.text.strip()
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
