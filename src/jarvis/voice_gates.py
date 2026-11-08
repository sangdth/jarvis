from __future__ import annotations

import asyncio
import os
import time
import wave
from collections.abc import AsyncGenerator, Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np
from loguru import logger
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    Frame,
    ProposedUserStartedSpeakingFrame,
    ProposedUserStoppedSpeakingFrame,
    StartFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.whisper.stt import WhisperSTTServiceMLX

from jarvis.config import JARVIS_HOME, settings
from jarvis.executive_filter import ExecutiveFilter
from jarvis.speaker import SpeakerEmbedder, cosine
from jarvis.state import JarvisMode, JarvisState, UserIntent, classify_fast_command, is_not_for_jarvis

# Whisper ends a sentence that trails off mid-thought with one of these ("Open the...").
TRAILING_OFF = ("...", "…", "-", "—")
# How long a trailing-off sentence waits for the rest, counted from the last stretch of silence.
HOLD_SECS = 2.0
# A sentence this soon after Jarvis stopped talking is judged as a possible answer to what it said.
REPLY_WINDOW_SECS = 20.0
# SAVE_SEGMENTS keeps the newest sentences here, to re-run Whisper and the voice check on real misses.
CLIPS_DIR = JARVIS_HOME / "clips"


def save_clip(pcm16: bytes, rate: int, keep: int, folder: Path = CLIPS_DIR) -> Path | None:
    """Write one sentence as a WAV only the user can read; delete the oldest past `keep`. None if the disk fails."""
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / f"{datetime.now():%Y%m%d-%H%M%S-%f}.wav"
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
            with wave.open(file, "wb") as clip:
                clip.setnchannels(1)
                clip.setsampwidth(2)
                clip.setframerate(rate)
                clip.writeframes(pcm16)
        for old in sorted(folder.glob("*.wav"))[:-keep]:  # names sort by time
            old.unlink(missing_ok=True)
        return path
    except OSError as exc:
        logger.warning(f"Could not save the sentence to {folder} ({exc!r}).")
        return None


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
        # Saved before any drop: the misses worth replaying are the ones Whisper or the voice check threw away.
        clip = save_clip(audio, self.sample_rate, settings.save_segments) if settings.save_segments > 0 else None
        saved = f" ({clip.name})" if clip else ""
        if clip and not any(isinstance(f, TranscriptionFrame) for f in frames):
            logger.info(f"[Speaker] score={score:.2f} no text{saved}")  # Whisper heard noise or nothing
        for frame in frames:
            if isinstance(frame, TranscriptionFrame):
                owner = score >= self._threshold
                passed = owner or classify_fast_command(frame.text) is not None
                logger.info(f"[Speaker] score={score:.2f} {'pass' if passed else 'drop'}: {frame.text!r}{saved}")
                if not passed:
                    continue
            yield frame

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        # mlx_whisper loads the weights on the first call; do it now so the user's first sentence isn't the slow one.
        import mlx_whisper

        await asyncio.to_thread(
            mlx_whisper.transcribe, np.zeros(16000, dtype=np.float32), path_or_hf_repo=self._settings.model  # type: ignore[attr-defined]
        )

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

    def __init__(
        self,
        state: JarvisState,
        executive_filter: ExecutiveFilter,
        last_said: Callable[[], str | None] = lambda: None,
    ) -> None:
        super().__init__()
        self.state = state
        self.executive_filter = executive_filter
        # What Jarvis said last (the conversation model's last reply), for answers to its own questions.
        self._last_said = last_said
        self._bot_stopped_at: float | None = None
        # A sentence that trailed off ("Open the..."), waiting for the rest after a pause.
        self._held: TranscriptionFrame | None = None
        self._hold_timer: asyncio.Task | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if not isinstance(frame, TranscriptionFrame):
            if isinstance(frame, BotStoppedSpeakingFrame):
                self._bot_stopped_at = time.monotonic()
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
            held = self._held
            if held is not None:
                self._cancel_hold_timer()
                frame = replace(frame, text=f"{held.text.strip()} {transcript}")
                self._held = None
            # Hold once: a joined sentence that trails off again goes on now instead of waiting another HOLD_SECS.
            if held is None and frame.text.strip().endswith(TRAILING_OFF):
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

    def _recent_jarvis_question(self) -> str | None:
        """An open agent question first; else Jarvis's own last reply, if it stopped talking moments ago."""
        topic = next((t.summary for t in reversed(list(self.state.active_agent_context.values()))), None)
        if topic is not None:
            return topic
        recent = self._bot_stopped_at is not None and time.monotonic() - self._bot_stopped_at < REPLY_WINDOW_SECS
        return self._last_said() if recent else None

    async def _gate(self, frame: TranscriptionFrame, direction: FrameDirection) -> None:
        transcript = frame.text.strip()
        logger.info(f"[User Transcript] ({self.state.mode.value}): {transcript!r}")

        # While muted only "you can talk" matters; skip the paid LLM call for everything else.
        if self.state.mode == JarvisMode.BEEP_ONLY and classify_fast_command(transcript) is None:
            return

        # "Not you": Jarvis took room talk as a turn, so it stops answering it. The filter logs the correction.
        if is_not_for_jarvis(transcript) and self.state.on_interrupt:
            await self.state.on_interrupt()

        intent = await self.executive_filter.classify_user_utterance(
            transcript=transcript,
            recent_jarvis_question=self._recent_jarvis_question(),
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


def last_assistant_text(messages: list) -> str | None:
    """The text of the newest assistant message in an LLM context, or None."""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if isinstance(content, str) and content.strip():
            return content.strip()[-500:]
    return None
