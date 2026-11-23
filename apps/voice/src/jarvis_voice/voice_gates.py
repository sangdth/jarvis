from __future__ import annotations

import asyncio
import functools
import os
import re
import time
import wave
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger
from pipecat.audio.turn.base_turn_analyzer import BaseTurnAnalyzer, EndOfTurnState
from pipecat.frames.frames import (
    BotSpeakingFrame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    ProposedUserStartedSpeakingFrame,
    ProposedUserStoppedSpeakingFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from jarvis_voice import telemetry
from jarvis_voice.cleanup import TranscriptCleaner
from jarvis_voice.config import JARVIS_HOME, WHISPER_MODELS, settings
from jarvis_voice.executive_filter import ExecutiveFilter
from jarvis_voice.prompts import LANGUAGES, MEETING_TRANSCRIPT, meeting_mode
from jarvis_voice.speaker import (
    KNOWN_AT,
    TENTATIVE_AT,
    SpeakerBeliefTracker,
    SpeakerEmbedder,
    SpeakerRegistry,
    VoiceProfile,
    cosine,
    speaker_number,
)
from jarvis_voice.state import JarvisMode, JarvisState, UserIntent, classify_fast_command, is_not_for_jarvis

# Longest sentence Smart Turn keeps open across pauses; past this it goes to Whisper as it is.
MAX_HELD_SECS = 30
# Whisper ends a sentence cut off mid-thought with trailing punctuation or a dangling mid-clause connector.
TRAILING_OFF = ("...", "…", "-", "—", ",", ":", ";")
_TRAILING_CONNECTOR = re.compile(
    r"\b(?:and|or|but|because|although|unless|until|into|onto|the|và|hoặc|nhưng|bởi\s+vì)[.]?$",
    re.IGNORECASE,
)
# How long a trailing-off sentence waits for the rest, counted from the last stretch of silence.
HOLD_SECS = 2.0
# After VAD speech stops while a verified turn was waiting on continued speech, wait briefly for Whisper's segment.
SPEECH_SETTLE_SECS = 0.8
# Hard ceiling on holding a ready turn when background sound keeps VAD active.
MAX_READY_HOLD_SECS = 8.0
# A sentence this soon after Jarvis stopped talking is judged as a possible answer to what it said.
REPLY_WINDOW_SECS = 20.0
# A voice the owner just introduced may answer for this long, then the room is background again.
SOCIAL_WINDOW_SECS = 90.0
# Ordinary owner speech makes the next sentence answerable by whoever the owner is talking to.
ATTENDED_WINDOW_SECS = 30.0
# SAVE_SEGMENTS keeps the newest sentences here, to re-run Whisper and the voice check on real misses.
CLIPS_DIR = JARVIS_HOME / "clips"
# How many recent meeting utterances to hold and pass as context the next time Jarvis is addressed.
MEETING_LOG_LIMIT = 40
# In a meeting, people talk to each other by default; unless Jarvis just asked a question, it only answers when named.
_MEETING_ADDRESS_PATTERNS = re.compile(r"\b(jarvis|javis|jarvis's)\b", re.IGNORECASE)
ACTIVATION_SOUND_PATH = Path(__file__).parent / "defaults" / "sound-activation.wav"
# Play the low activation sound right before Jarvis speaks once the conversation has stopped for at least this long.
ACTIVATION_BOUNCE_SECS = 60.0
ACTIVATION_VOLUME = 0.25


def is_meeting_addressed(transcript: str) -> bool:
    """True when a sentence in a meeting names Jarvis directly."""
    return _MEETING_ADDRESS_PATTERNS.search(transcript) is not None


def is_trailing_off(transcript: str) -> bool:
    """True when a transcribed clause stops on trailing punctuation or an open mid-clause connector."""
    text = transcript.strip()
    if not text:
        return False
    if text.endswith(TRAILING_OFF):
        return True
    if text.endswith(("?", "!")):
        return False
    return _TRAILING_CONNECTOR.search(text) is not None


def _uninterruptible(frame: TranscriptionFrame, *, smart_turn_held: bool | None = None) -> TranscriptionFrame:
    """User speech must not be dropped or cancelled when an InterruptionFrame travels upstream."""
    frame.interruptible = False
    if smart_turn_held is not None:
        frame._smart_turn_held = smart_turn_held  # type: ignore[attr-defined]
    return frame


def _keep_speaker_audio(target: TranscriptionFrame, source: TranscriptionFrame) -> TranscriptionFrame:
    """`replace()` drops attributes added by the STT; a joined sentence keeps its duration and pitch."""
    for name in ("_pitch_hz", "_speech_secs"):
        if hasattr(source, name):
            setattr(target, name, getattr(source, name))
    return target


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


@dataclass
class WhisperMLXSettings(STTSettings):
    no_speech_prob: float = 0.6
    temperature: float = 0.0


class SpeakerVerifiedWhisperSTT(SegmentedSTTService):
    """
    MLX Whisper that passes on the master's sentences, known enrolled speakers, or a new speaker being enrolled
    (or tags owner / known speaker / room in meeting mode).
    Pipecat hands run_stt one whole VAD sentence; the voice score runs alongside Whisper, so it adds no delay.
    Every heard non-owner voice builds a fingerprint profile in `SpeakerRegistry`.
    Mute / unmute phrases pass from any voice: the worst case is that someone else silences Jarvis.
    Without a voiceprint (MASTER_CHECK off) nothing is scored and every sentence counts as the master's.
    """

    Settings = WhisperMLXSettings
    _settings: WhisperMLXSettings

    @property
    def wants_wav_segments(self) -> bool:
        return False

    def can_generate_metrics(self) -> bool:
        return True

    def __init__(
        self,
        *,
        embedder: SpeakerEmbedder | None,
        voiceprint: np.ndarray | None,
        threshold: float,
        meeting: bool = False,
        registry: SpeakerRegistry | None = None,
        state: JarvisState | None = None,
        turn_analyzer: BaseTurnAnalyzer | None = None,
        settings: WhisperMLXSettings | None = None,
        **kwargs: Any,
    ) -> None:
        default_settings = self.Settings(model=WHISPER_MODELS["en"], language=Language.EN)
        if settings is not None:
            default_settings.apply_update(settings)
        super().__init__(settings=default_settings, **kwargs)
        self._embedder = embedder
        self._voiceprint = voiceprint
        self._threshold = threshold
        self._meeting = meeting
        self._registry = registry if registry is not None else SpeakerRegistry(folder=None)
        self._state = state
        # Smart Turn (SMART_TURN): judges from the sound whether a pause ends the sentence. A pause mid-thought keeps
        # the sentence open, so Whisper and the voice check get it whole instead of in two halves.
        self._turn = turn_analyzer
        self._vad_speaking = False
        self._holding = False
        self._smart_turn_held = False

    async def _handle_user_started_speaking(self, frame: VADUserStartedSpeakingFrame) -> None:
        self._vad_speaking = True
        self._holding = False  # the rest of a held sentence: it joins the same buffer
        self._smart_turn_held = False
        await super()._handle_user_started_speaking(frame)

    async def _handle_user_stopped_speaking(self, frame: VADUserStoppedSpeakingFrame) -> None:
        self._vad_speaking = False
        if self._turn is not None:
            done, _ = await self._turn.analyze_end_of_turn()
            # A TV or a meeting that never sounds done would grow the buffer for minutes, then stall Whisper on it.
            if done == EndOfTurnState.INCOMPLETE:
                if len(self._audio_buffer) < self.sample_rate * 2 * MAX_HELD_SECS:
                    # STTService._handle_vad_user_stopped_speaking already set _user_speaking = False, which makes
                    # SegmentedSTTService trim _audio_buffer to 1 s during the pause; keep it True while held.
                    self._user_speaking = True
                    self._holding = True
                    logger.debug("[Turn] pause mid-sentence; waiting for the rest")
                    return
                self._turn.clear()
        self._holding = False
        self._smart_turn_held = False
        await super()._handle_user_stopped_speaking(frame)

    async def process_audio_frame(self, frame: InputAudioRawFrame, direction: FrameDirection) -> None:
        await super().process_audio_frame(frame, direction)
        if self._turn is None:
            return
        # COMPLETE here means silence ran past SmartTurnParams.stop_secs: a held sentence goes on as it is. Straight
        # to Pipecat's flush: the analyzer has cleared its audio, so asking it again would hold the sentence forever.
        if self._turn.append_audio(frame.audio, self._vad_speaking) == EndOfTurnState.COMPLETE and self._holding:
            self._holding = False
            self._smart_turn_held = True
            await super()._handle_user_stopped_speaking(VADUserStoppedSpeakingFrame())

    async def _transcribe_mlx(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        try:
            import mlx_whisper

            await self.start_processing_metrics()
            audio_float = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
            language = Language(self._settings.language) if self._settings.language else None
            chunk = await asyncio.to_thread(
                mlx_whisper.transcribe,
                audio_float,
                path_or_hf_repo=self._settings.model,
                temperature=self._settings.temperature,
                language=str(language) if language else None,
            )
            text = ""
            for segment in chunk.get("segments", []):
                if segment.get("compression_ratio") == 0.5555555555555556:
                    continue
                if segment.get("no_speech_prob", 0.0) < self._settings.no_speech_prob:
                    text += f"{segment.get('text', '')} "
            await self.stop_processing_metrics()
            if text.strip():
                logger.debug(f"Transcription: [{text}]")
                yield _uninterruptible(TranscriptionFrame(text, self._user_id, time_now_iso8601(), language))
        except Exception as exc:
            yield ErrorFrame(error=f"Unknown error occurred: {exc}")

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        started = time.monotonic()
        held_by_turn, self._smart_turn_held = self._smart_turn_held, False
        checking = self._embedder is not None and self._voiceprint is not None
        score_task = asyncio.create_task(asyncio.to_thread(self._analyze, audio)) if checking else None
        frames = [frame async for frame in self._transcribe_mlx(audio)]
        score, profile, pitch_hz, speech_secs = await score_task if score_task else (None, None, None, 0.0)
        heard = f"score={score:.2f}" if score is not None else "off"
        owner = score is None or score >= self._threshold
        # Saved before any drop: the misses worth replaying are the ones Whisper or the voice check threw away.
        clip = (
            await asyncio.to_thread(save_clip, audio, self.sample_rate, settings.save_segments)
            if settings.save_segments > 0
            else None
        )
        saved = f" ({clip.name})" if clip else ""
        if clip and not any(isinstance(f, TranscriptionFrame) for f in frames):
            logger.info(f"[Speaker] {heard} no text{saved}")  # Whisper heard noise or nothing
        # Read live: "meeting mode on" by voice changes the tagging from the next sentence, mid-run.
        meeting = self._state.meeting if self._state is not None else self._meeting
        social = self._state is not None and self._state.social_window_until > time.monotonic()
        for frame in frames:
            if isinstance(frame, TranscriptionFrame):
                ms = round((time.monotonic() - started) * 1000)
                # One turn per sentence: every stage after this lands under the same id in the cockpit.
                telemetry.new_turn()
                known = profile is not None and profile.enrolled and profile.name is not None
                if meeting:
                    if owner:
                        role = "owner"
                    elif known and profile is not None and profile.name:
                        role = profile.name
                    elif profile is not None:
                        role = profile.id
                    else:
                        role = "room"
                    logger.info(f"[Speaker] {heard} {role}: {frame.text!r}{saved}")
                    telemetry.emit(
                        "voice.stt", text=frame.text, score=score, threshold=self._threshold,
                        passed=True, role=role, ms=ms, clip=clip.name if clip else "",
                    )
                    frame = replace(frame, user_id=role)
                else:
                    fast = classify_fast_command(frame.text) in (UserIntent.MUTE, UserIntent.UNMUTE)
                    # An unnamed voice is heard only while the owner may be talking to it; a day of TV is dropped.
                    passed = owner or known or (profile is not None and social) or fast
                    logger.info(f"[Speaker] {heard} {'pass' if passed else 'drop'}: {frame.text!r}{saved}")
                    telemetry.emit(
                        "voice.stt", text=frame.text, score=score, threshold=self._threshold,
                        passed=passed, role="owner" if owner else (profile.name if known and profile else "guest"),
                        ms=ms, clip=clip.name if clip else "",
                    )
                    if not passed:
                        continue
                    if not owner and profile is not None:
                        frame = replace(frame, user_id=profile.name if known and profile.name else profile.id)
                setattr(frame, "_pitch_hz", pitch_hz)
                setattr(frame, "_speech_secs", speech_secs)
                frame = _uninterruptible(frame, smart_turn_held=held_by_turn)
            yield frame

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if self._turn is not None:
            self._turn.set_sample_rate(self.sample_rate)
        # mlx_whisper loads the weights on the first call; do it now so the user's first sentence isn't the slow one.
        import mlx_whisper

        await asyncio.to_thread(
            mlx_whisper.transcribe,
            np.zeros(16000, dtype=np.float32),
            path_or_hf_repo=self._settings.model,
            language=str(self._settings.language) if self._settings.language else None,
            temperature=0.0,
        )

    async def cleanup(self) -> None:
        if self._turn is not None:
            await self._turn.cleanup()
        await super().cleanup()

    def _analyze(self, audio: bytes) -> tuple[float, VoiceProfile | None, float | None, float]:
        assert self._embedder is not None and self._voiceprint is not None  # only called with the master check on
        stats = self._embedder.analyze(audio)
        if stats is None:
            return 0.0, None, None, 0.0
        score = cosine(stats.embedding, self._voiceprint)
        if score >= self._threshold:
            return score, None, stats.pitch_hz, stats.speech_secs
        # Every unnamed voice gets a RAM-only fingerprint that expires: recognition improves while nothing piles up.
        profile = self._registry.observe(stats.embedding, self._threshold, stats.speech_secs)
        return score, profile, stats.pitch_hz, stats.speech_secs


class JarvisIntentGate(FrameProcessor):
    """
    Pipecat FrameProcessor placed right after local STT (MLX Whisper).
    1. Immediately catches 'Shut up' / 'Be quiet' -> switches Jarvis to BEEP_ONLY mode.
    2. Immediately catches 'You can talk' -> restores VOICE mode and reads queued topics.
    3. When in BEEP_ONLY mode, suppresses all normal conversational responses.
    4. When the master's intent is INTRODUCTION, offers that name to the room so the next voice to answer is matched
       to it with a confidence, no scripted questions; a confidently named voice is remembered by itself.
    5. When in VOICE mode, runs the Always-On Turn Classifier so Jarvis only responds
       when actually needed (no wake word required; in meeting mode, buffers room talk until Jarvis is addressed).
    """

    def __init__(
        self,
        state: JarvisState,
        executive_filter: ExecutiveFilter,
        last_said: Callable[[], str | None] = lambda: None,
        *,
        meeting: bool = False,
        registry: SpeakerRegistry | None = None,
        cleaner: TranscriptCleaner | None = None,
    ) -> None:
        super().__init__()
        self.state = state
        self.executive_filter = executive_filter
        self.cleaner = cleaner  # None: sentences go on as Whisper heard them
        # What Jarvis said last (the conversation model's last reply), for answers to its own questions.
        self._last_said = last_said
        # `jarvis meeting` at startup; from then on `state.meeting` is the one source of truth (voice can toggle it).
        if meeting:
            state.meeting = True
        self.registry = registry if registry is not None else SpeakerRegistry(folder=None)
        self.tracker = SpeakerBeliefTracker()
        self._meeting_log: list[str] = []
        self._bot_speaking = False
        self._bot_stopped_at: float | None = None
        self._vad_speaking = False
        # A sentence that trailed off ("Open the..."), waiting for the rest after a pause.
        self._held: TranscriptionFrame | None = None
        self._hold_timer: asyncio.Task | None = None
        self._released: TranscriptionFrame | None = None
        # A verified turn held briefly when the user has already started speaking the next clause.
        self._ready: TranscriptionFrame | None = None
        self._ready_started_at: float = 0.0
        self._ready_timer: asyncio.Task | None = None
        self._flushing_ready: TranscriptionFrame | None = None
        self._in_flight: asyncio.Task[None] | None = None

    async def queue_frame(
        self,
        frame: Frame,
        direction: FrameDirection = FrameDirection.DOWNSTREAM,
        callback: Callable[..., Any] | None = None,
    ) -> None:
        if (
            isinstance(frame, TranscriptionFrame)
            and self._in_flight is not None
            and not self._in_flight.done()
            and classify_fast_command(frame.text.strip()) in (UserIntent.MUTE, UserIntent.UNMUTE)
        ):
            self._in_flight.cancel()
        await super().queue_frame(frame, direction, callback)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if not isinstance(frame, TranscriptionFrame):
            if isinstance(frame, BotStartedSpeakingFrame):
                self._bot_speaking = True
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self._bot_speaking = False
                self._bot_stopped_at = time.monotonic()
            elif isinstance(frame, VADUserStartedSpeakingFrame):
                self._vad_speaking = True
                self.state.user_speaking = True
                if self._held is not None:
                    self._cancel_hold_timer()
                if self._ready is not None:
                    remaining = max(0.05, MAX_READY_HOLD_SECS - (time.monotonic() - self._ready_started_at))
                    self._start_ready_timer(direction, remaining)
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                self._vad_speaking = False
                self.state.user_speaking = False
                if self._held is not None:
                    self._start_hold_timer(direction)
                elif self._ready is not None:
                    self._start_ready_timer(direction, SPEECH_SETTLE_SECS)
            await self.push_frame(frame, direction)
            return

        _uninterruptible(frame)
        if frame is self._flushing_ready:
            self._flushing_ready = None
            if self._ready is frame:
                self._ready = None
                self._cancel_ready_timer()
                await self._emit_turn(frame, direction)
            return

        transcript = frame.text.strip()
        if not transcript:
            return

        if frame is self._released:
            self._released = None
            await self._run_gate(frame, direction)
            return

        fast = classify_fast_command(transcript)
        if fast in (UserIntent.MUTE, UserIntent.UNMUTE):
            self._cancel_hold_timer()
            self._held = None
            self._cancel_ready_timer()
            self._ready = None
            if self._in_flight is not None and not self._in_flight.done():
                self._in_flight.cancel()
        elif self._ready is not None and self._ready.user_id != frame.user_id:
            await self._flush_ready_now(direction)
        # Mute / unmute / introduction act at once and never join a held sentence.
        if fast is None:
            held = self._held
            if held is not None and held.user_id == frame.user_id:
                self._cancel_hold_timer()
                joined = replace(frame, text=f"{held.text.strip()} {transcript}")
                frame = _uninterruptible(_keep_speaker_audio(joined, frame))
                self._held = None
            # Hold once unless Smart Turn already waited out its silence cap: catches brief mid-thought pauses.
            elif held is None and not getattr(frame, "_smart_turn_held", False) and is_trailing_off(transcript):
                logger.info(f"[User Transcript] holding for the rest: {transcript!r}")
                self._held = frame
                self._cancel_ready_timer()
                if not self._vad_speaking:
                    self._start_hold_timer(direction)
                return

        await self._run_gate(frame, direction)

    async def _run_gate(self, frame: TranscriptionFrame, direction: FrameDirection) -> None:
        task = asyncio.create_task(self._gate(frame, direction))
        self._in_flight = task
        try:
            await task
        except asyncio.CancelledError:
            if (current := asyncio.current_task()) is not None and current.cancelling():
                raise
        finally:
            if self._in_flight is task:
                self._in_flight = None

    async def cleanup(self) -> None:
        self._cancel_hold_timer()
        self._cancel_ready_timer()
        self._held = None
        self._ready = None
        self.state.user_speaking = False
        if self._in_flight is not None:
            self._in_flight.cancel()
            self._in_flight = None
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
            # Re-enter via queue_frame so the released frame serializes with incoming frames on FrameProcessor's queue.
            self._released = frame
            await self.queue_frame(frame, direction)

    def _start_ready_timer(self, direction: FrameDirection, delay: float) -> None:
        self._cancel_ready_timer()
        self._ready_timer = asyncio.create_task(self._release_ready(direction, delay))

    def _cancel_ready_timer(self) -> None:
        if self._ready_timer is not None:
            self._ready_timer.cancel()
            self._ready_timer = None

    async def _release_ready(self, direction: FrameDirection, delay: float) -> None:
        """Flush a verified turn once follow-up speech settles (or hits MAX_READY_HOLD_SECS)."""
        await asyncio.sleep(delay)
        frame, self._ready_timer = self._ready, None
        if frame is not None and (self._held is None or self._held.user_id != frame.user_id):
            self._flushing_ready = frame
            await self.queue_frame(frame, direction)

    async def _flush_ready_now(self, direction: FrameDirection) -> None:
        if self._ready is not None:
            ready, self._ready = self._ready, None
            self._cancel_ready_timer()
            await self._emit_turn(ready, direction)

    def _recent_jarvis_question(self) -> str | None:
        """An open agent question first; else Jarvis's own last reply, if it stopped talking moments ago."""
        topic = next((t.summary for t in reversed(list(self.state.active_agent_context.values()))), None)
        if topic is not None:
            return topic
        recent = self._bot_stopped_at is not None and time.monotonic() - self._bot_stopped_at < REPLY_WINDOW_SECS
        return self._last_said() if recent else None

    def _buffer_meeting(self, speaker: str, transcript: str) -> None:
        if classify_fast_command(transcript) == UserIntent.IGNORE_BACKGROUND or is_not_for_jarvis(transcript):
            return
        self._meeting_log.append(f"[{speaker}]: {transcript}")
        if len(self._meeting_log) > MEETING_LOG_LIMIT:
            del self._meeting_log[: len(self._meeting_log) - MEETING_LOG_LIMIT]
        logger.info(f"[Meeting] buffered ({speaker}): {transcript!r}")

    async def _set_meeting_mode(self, on: bool, *, is_owner: bool) -> None:
        """
        "Meeting mode on" / "meeting mode off", by voice.

        Only the master may change it: with the master check on, a guest asking would otherwise be able to turn the
        room-tagging off and speak as the user. The rules are handed to the conversation model as a system note, so
        the change holds from the next turn without restarting the pipeline.
        """
        if settings.master_check and not is_owner:
            logger.info("[Meeting] ignored a guest asking to change meeting mode.")
            return
        line = LANGUAGES[settings.language]["meeting_on" if on else "meeting_off"]
        if self.state.meeting == on:
            if self.state.on_speak:
                await self.state.on_speak(line)  # already there: just confirm, no second rule injection
            return
        await self.state.set_meeting(on)
        self._meeting_log.clear()
        self.state.room_turn = False
        if on:
            await self.state.notify(
                "Meeting mode is now ON.\n" + meeting_mode(True, guests=settings.master_check).strip()
            )
        else:
            await self.state.notify(
                "Meeting mode is now OFF. Only the user is speaking again; no one is tagged [Room] or a guest."
            )
        logger.info(f"[Meeting] mode {'on' if on else 'off'} (by voice)")
        if self.state.on_speak:
            await self.state.on_speak(line)

    def _resolve_profile(self, user_id: str) -> VoiceProfile | None:
        if user_id in ("owner", "u", "", "room"):
            return None
        # None for an id the registry no longer has (merged into a named profile): the sentence counts as unknown.
        return self.registry.get(user_id)

    def _speaker_tag(self, frame: TranscriptionFrame, transcript: str) -> tuple[str, float | None, str]:
        """The `[Speaker: ...]` tag, its confidence, and the name the cockpit shows for this turn."""
        if frame.user_id in ("owner", "u", ""):
            return "User", None, "Owner"
        profile = self._resolve_profile(frame.user_id)
        if profile is None:
            return ("Room", None, "Room") if self.state.meeting else ("Speaker: Guest", None, "Guest")
        if profile.enrolled and profile.name:
            return f"Speaker: {profile.name}", 1.0, profile.name
        belief = self.tracker.observe_guest(
            profile,
            transcript,
            pitch_hz=getattr(frame, "_pitch_hz", None),
            now=time.monotonic(),
            meeting=self.state.meeting,
            registry=self.registry,
        )
        display = belief.name or f"Speaker {speaker_number(profile)}"
        if belief.name and belief.confidence >= KNOWN_AT:
            return f"Speaker: {belief.name}", belief.confidence, belief.name
        if belief.name and belief.confidence >= TENTATIVE_AT:
            # One light check next turn confirms it: the executive model greets them by the guessed name.
            self.tracker.last_asked = (profile.id, belief.name)
            return f"Speaker: {belief.name}? ({belief.confidence * 100:.0f}%)", belief.confidence, display
        if self.state.meeting:
            return "Room", belief.confidence, display
        return "Speaker: Guest", belief.confidence, display

    async def _gate(self, frame: TranscriptionFrame, direction: FrameDirection) -> None:
        transcript = frame.text.strip()
        is_owner = frame.user_id in ("owner", "u", "")
        profile = self._resolve_profile(frame.user_id)
        logger.info(f"[User Transcript] ({self.state.mode.value}): {transcript!r}")

        now = time.monotonic()
        fast = classify_fast_command(transcript)
        if fast is not None and fast != UserIntent.IGNORE_BACKGROUND:
            # Mute, unmute and the meeting switch settle in code, with no model call and no latency.
            telemetry.emit("voice.fastcmd", command=fast.value, text=transcript)
        if is_owner:
            # The owner speaking is what makes a nearby voice answerable: the next sentence may be theirs.
            self.state.social_window_until = max(self.state.social_window_until, now + ATTENDED_WINDOW_SECS)
            self.tracker.owner_turn(
                transcript, [p.name for p in self.registry.profiles.values() if p.name], now
            )
        elif frame.user_id != "room" and profile is None:
            if fast not in (UserIntent.MUTE, UserIntent.UNMUTE):
                logger.info(f"[Speaker] dropped unknown voice {frame.user_id!r}: {transcript!r}")
                return
        elif profile is not None and not profile.enrolled and not self.state.meeting:
            if now >= self.state.social_window_until and fast not in (UserIntent.MUTE, UserIntent.UNMUTE):
                logger.info(f"[Speaker] dropped parked voice {frame.user_id!r}: {transcript!r}")
                return
        # The same label in both modes, so memory skips a guest's words ([Speaker: David]) like it skips [Room].
        speaker = "User" if is_owner else (f"Speaker: {profile.name}" if profile and profile.name else "Room")

        # "Meeting mode on / off": deterministic, no model call, and it works while muted (it is about who is heard).
        if fast in (UserIntent.MEETING_ON, UserIntent.MEETING_OFF):
            await self._flush_ready_now(direction)
            await self._set_meeting_mode(fast is UserIntent.MEETING_ON, is_owner=is_owner)
            return

        # While muted only "you can talk" matters; skip the paid LLM call for everything else.
        if self.state.mode == JarvisMode.BEEP_ONLY and fast not in (UserIntent.MUTE, UserIntent.UNMUTE):
            if self.state.meeting:
                self._buffer_meeting(speaker, transcript)
            return

        # "Not you": Jarvis took room talk as a turn, so it stops answering it. The filter logs the correction.
        if is_not_for_jarvis(transcript) and self.state.on_interrupt:
            await self.state.on_interrupt()

        question = self._recent_jarvis_question()
        open_question = question is not None and (bool(self.state.active_agent_context) or "?" in question)
        # In a meeting only the user may answer Jarvis without naming it, and only when Jarvis left a question open:
        # a plain statement ("Added to your calendar.") must not let subsequent talk bypass buffering.
        if (
            self.state.meeting
            and fast is None
            and not is_not_for_jarvis(transcript)
            and (not open_question or not is_owner)
            and not is_meeting_addressed(transcript)
        ):
            self._buffer_meeting(speaker, transcript)
            if not self._vad_speaking and self._held is None and not self.has_queued_frame(TranscriptionFrame):
                await self._flush_ready_now(direction)
            return

        asked_at = time.monotonic()
        intent = await self.executive_filter.classify_user_utterance(
            transcript=transcript,
            recent_jarvis_question=question,
        )
        intent_ms = round((time.monotonic() - asked_at) * 1000)
        # Pipecat's `[Latency]` lines count this inside transcription (the gate re-sends the transcript), so say it here.
        logger.info(f"[Intent] {intent.value} in {(time.monotonic() - asked_at) * 1000:.0f} ms")
        telemetry.emit(
            "voice.intent",
            intent=intent.value,
            ms=intent_ms,
            speaker=speaker,
            owner=is_owner,
            meeting=self.state.meeting,
            backdrop=telemetry.preview(question or "", 160),
        )

        if intent == UserIntent.MUTE:
            self._cancel_ready_timer()
            self._ready = None
            logger.warning("User requested silence ('Shut up' / 'Be quiet'). Entering BEEP_ONLY mode.")
            await self.state.set_mute()
            return

        if intent == UserIntent.UNMUTE:
            self._cancel_ready_timer()
            self._ready = None
            logger.info("User restored voice mode ('You can talk').")
            await self.state.set_unmute()
            return

        if self.state.mode == JarvisMode.BEEP_ONLY:
            logger.debug("Jarvis is in BEEP_ONLY mode; ignoring non-unmute speech.")
            return

        if intent == UserIntent.INTRODUCTION:
            if not settings.master_check:
                # Without the master check voices are not told apart, so the conversation model just says hello.
                intent = UserIntent.ADDRESSED_TO_JARVIS
            elif is_owner:
                # The tracker already took the name; the turn still reaches the model, which greets the newcomer.
                self.state.social_window_until = max(
                    self.state.social_window_until, time.monotonic() + SOCIAL_WINDOW_SECS
                )
                if not any(p.name for p in self.tracker.pending):
                    self.tracker.note_intro(None, None)
                logger.info("[Speaker] master introduced someone; the next new voice may claim the name.")
            else:
                return

        if intent == UserIntent.IGNORE_BACKGROUND:
            if self.state.meeting:
                self._buffer_meeting(speaker, transcript)
            else:
                logger.debug(f"Ignored self-talk / unaddressed speech: {transcript!r}")
            if not self._vad_speaking and self._held is None and not self.has_queued_frame(TranscriptionFrame):
                await self._flush_ready_now(direction)
            return

        if self._bot_speaking and (self.cleaner is not None or self._vad_speaking):
            # Stop playback right away when cleaning up or holding for continued speech so Jarvis never talks over it.
            self._bot_speaking = False
            await self.push_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)

        if self.cleaner is not None:
            # After the speech check (it and its feedback log need the words as heard), before the speaker labels.
            # An open agent question or approval is agent text (maybe from a web page): it never steers the rewrite.
            context = None if self.state.active_agent_context else question
            cleaned_at = time.monotonic()
            heard_text = transcript
            cleaned = await self.cleaner.clean(transcript, recent_jarvis_question=context)
            ms = (time.monotonic() - cleaned_at) * 1000
            change = f": {transcript!r} -> {cleaned!r}" if cleaned != transcript else " (unchanged)"
            logger.info(f"[Cleanup] {ms:.0f} ms{change}")
            telemetry.emit(
                "voice.cleanup", raw=heard_text, text=cleaned, ms=round(ms), changed=cleaned != heard_text
            )
            transcript = cleaned
            frame = _uninterruptible(_keep_speaker_audio(replace(frame, text=transcript), frame))

        if self._ready is not None:
            transcript = f"{self._ready.text.strip()} {transcript}"
            frame = _uninterruptible(_keep_speaker_audio(replace(frame, text=transcript), self._ready))
        else:
            self._ready_started_at = time.monotonic()

        elapsed = time.monotonic() - self._ready_started_at
        same_held = self._held is not None and self._held.user_id == frame.user_id
        if (
            self._vad_speaking or same_held or self.has_queued_frame(TranscriptionFrame)
        ) and elapsed < MAX_READY_HOLD_SECS:
            logger.info(f"[User Transcript] user still speaking; holding turn: {transcript!r}")
            self._ready = frame
            self._start_ready_timer(direction, max(0.05, MAX_READY_HOLD_SECS - elapsed))
            return

        self._ready = None
        self._cancel_ready_timer()
        await self._emit_turn(frame, direction)

    async def _emit_turn(self, frame: TranscriptionFrame, direction: FrameDirection) -> None:
        transcript = frame.text.strip()
        is_owner = frame.user_id in ("owner", "u", "")
        profile = self._resolve_profile(frame.user_id)
        tag, confidence, display = self._speaker_tag(frame, transcript)
        speaker = "User" if is_owner else (f"Speaker: {display}" if display not in ("Room", "Guest") else tag)
        self.state.room_turn = not is_owner
        self.state.current_speaker = speaker
        telemetry.emit(
            "voice.speaker",
            label=display,
            confidence=confidence,
            known=is_owner or (confidence is not None and confidence >= KNOWN_AT),
            voice_id=frame.user_id,
        )
        profile = self._resolve_profile(frame.user_id) or profile
        if profile is not None and profile.enrolled and profile.name and not profile.greeted:
            # Once per run; the request itself still goes on to the model below.
            profile.greeted = True
            if self.state.on_speak:
                await self.state.on_speak(LANGUAGES[settings.language]["speaker_greeting"].format(name=profile.name))

        if self.state.meeting:
            history = "\n".join(self._meeting_log)
            self._meeting_log.clear()
            prefix = f"{MEETING_TRANSCRIPT}\n{history}]\n" if history else ""
            frame = _uninterruptible(replace(frame, text=f"{prefix}[{tag}]: {transcript}"))
        elif not is_owner:
            frame = _uninterruptible(replace(frame, text=f"[{tag}]: {transcript}"))

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


@functools.lru_cache(maxsize=2)
def load_activation_pcm(volume: float = ACTIVATION_VOLUME) -> tuple[bytes, int]:
    """Load the shipped activation sound as 16-bit mono PCM bytes and its sample rate."""
    with wave.open(str(ACTIVATION_SOUND_PATH), "rb") as wav:
        rate = wav.getframerate()
        pcm = wav.readframes(wav.getnframes())
    if volume != 1.0:
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64) * volume
        pcm = np.clip(samples, -32767, 32767).astype("<i2").tobytes()
    return pcm, rate


class ActivationChime(FrameProcessor):
    """Plays a low activation sound right before Jarvis speaks when the conversation stopped for >= `bounce_secs`."""

    def __init__(
        self,
        *,
        bounce_secs: float = ACTIVATION_BOUNCE_SECS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._pcm, self._sample_rate = load_activation_pcm()
        self._chime_ns = round((len(self._pcm) / 2 / self._sample_rate) * 1_000_000_000)
        self._bounce_secs = bounce_secs
        self._clock = clock
        self._armed = True
        self._bot_speaking = False
        self._user_speaking = False
        self._last_active_at: float | None = None
        self._pts_shift_ns = 0

    def _rearm_if_idle(self, now: float) -> None:
        if (
            not self._bot_speaking
            and not self._user_speaking
            and self._last_active_at is not None
            and now - self._last_active_at >= self._bounce_secs
        ):
            self._armed = True

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        now = self._clock()

        if isinstance(frame, UserStartedSpeakingFrame):
            self._rearm_if_idle(now)
            self._user_speaking = True
            self._last_active_at = now
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._user_speaking = False
            self._last_active_at = now
        elif isinstance(frame, (BotStartedSpeakingFrame, BotSpeakingFrame)):
            self._bot_speaking = True
            self._last_active_at = now
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._pts_shift_ns = 0
            self._last_active_at = now
        elif isinstance(frame, InterruptionFrame):
            self._bot_speaking = False
            self._pts_shift_ns = 0
        elif direction == FrameDirection.DOWNSTREAM and isinstance(frame, TTSAudioRawFrame) and frame.audio:
            self._rearm_if_idle(now)
            if self._armed:
                self._armed = False
                self._pts_shift_ns = self._chime_ns
                await self.push_frame(
                    OutputAudioRawFrame(audio=self._pcm, sample_rate=self._sample_rate, num_channels=1),
                    direction,
                )
            self._last_active_at = now
        elif isinstance(frame, TTSStoppedFrame):
            if frame.pts and self._pts_shift_ns:
                frame.pts += self._pts_shift_ns
            self._pts_shift_ns = 0
        elif direction == FrameDirection.DOWNSTREAM and frame.pts and self._pts_shift_ns:
            frame.pts += self._pts_shift_ns

        await self.push_frame(frame, direction)
