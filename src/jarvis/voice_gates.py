from __future__ import annotations

import asyncio
import os
import re
import time
import wave
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, replace
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
from jarvis.prompts import LANGUAGES, MEETING_TRANSCRIPT
from jarvis.speaker import (
    SpeakerEmbedder,
    SpeakerRegistry,
    VoiceProfile,
    cosine,
    extract_introduced_name,
    extract_speaker_name,
)
from jarvis.state import JarvisMode, JarvisState, UserIntent, classify_fast_command, is_not_for_jarvis

# Whisper ends a sentence that trails off mid-thought with one of these ("Open the...").
TRAILING_OFF = ("...", "…", "-", "—")
# How long a trailing-off sentence waits for the rest, counted from the last stretch of silence.
HOLD_SECS = 2.0
# A sentence this soon after Jarvis stopped talking is judged as a possible answer to what it said.
REPLY_WINDOW_SECS = 20.0
# How long after the master's INTRODUCTION Jarvis waits for a new speaker to start talking or answer.
ENROLL_TIMEOUT_SECS = 120.0
# SAVE_SEGMENTS keeps the newest sentences here, to re-run Whisper and the voice check on real misses.
CLIPS_DIR = JARVIS_HOME / "clips"
# How many recent meeting utterances to hold and pass as context the next time Jarvis is addressed.
MEETING_LOG_LIMIT = 40
# In a meeting, people talk to each other by default; unless Jarvis just asked a question, it only answers when named.
_MEETING_ADDRESS_PATTERNS = re.compile(r"\b(jarvis|javis|jarvis's)\b", re.IGNORECASE)


@dataclass
class _Enrollment:
    started_at: float
    hint_name: str | None = None
    profile_id: str | None = None
    questions_asked: int = 0
    asked_name: bool = False


def is_meeting_addressed(transcript: str) -> bool:
    """True when a sentence in a meeting names Jarvis directly."""
    return _MEETING_ADDRESS_PATTERNS.search(transcript) is not None


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
    MLX Whisper that passes on the master's sentences, known enrolled speakers, or a new speaker being enrolled
    (or tags owner / known speaker / room in meeting mode).
    Pipecat hands run_stt one whole VAD sentence; the voice score runs alongside Whisper, so it adds no delay.
    Every heard non-owner voice builds a fingerprint profile in `SpeakerRegistry`.
    Mute / unmute phrases pass from any voice: the worst case is that someone else silences Jarvis.
    Without a voiceprint (MASTER_CHECK off) nothing is scored and every sentence counts as the master's.
    """

    def __init__(
        self,
        *,
        embedder: SpeakerEmbedder | None,
        voiceprint: np.ndarray | None,
        threshold: float,
        meeting: bool = False,
        registry: SpeakerRegistry | None = None,
        state: JarvisState | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._embedder = embedder
        self._voiceprint = voiceprint
        self._threshold = threshold
        self._meeting = meeting
        self._registry = registry if registry is not None else SpeakerRegistry(folder=None)
        self._state = state

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        checking = self._embedder is not None and self._voiceprint is not None
        score_task = asyncio.create_task(asyncio.to_thread(self._analyze, audio)) if checking else None
        frames = [frame async for frame in super().run_stt(audio)]
        emb, score = await score_task if score_task else (None, None)
        heard = f"score={score:.2f}" if score is not None else "off"
        owner = score is None or score >= self._threshold
        enrolling = self._state.enrolling if self._state is not None else False
        # Unknown voices get a profile only while the master is enrolling one; otherwise only enrolled voices match,
        # so a day of TV never grows the registry.
        profile = None
        if not owner:
            profile = self._registry.observe(emb, self._threshold) if enrolling else self._registry.match(emb, self._threshold)
        # Saved before any drop: the misses worth replaying are the ones Whisper or the voice check threw away.
        clip = save_clip(audio, self.sample_rate, settings.save_segments) if settings.save_segments > 0 else None
        saved = f" ({clip.name})" if clip else ""
        if clip and not any(isinstance(f, TranscriptionFrame) for f in frames):
            logger.info(f"[Speaker] {heard} no text{saved}")  # Whisper heard noise or nothing
        for frame in frames:
            if isinstance(frame, TranscriptionFrame):
                known = profile is not None and profile.enrolled and profile.name is not None
                if self._meeting:
                    if owner:
                        role = "owner"
                    elif known and profile is not None and profile.name:
                        role = profile.name
                    elif enrolling and profile is not None:
                        role = profile.id
                    else:
                        role = "room"
                    logger.info(f"[Speaker] {heard} {role}: {frame.text!r}{saved}")
                    frame = replace(frame, user_id=role)
                else:
                    fast = classify_fast_command(frame.text) in (UserIntent.MUTE, UserIntent.UNMUTE)
                    passed = owner or known or (enrolling and profile is not None) or fast
                    logger.info(f"[Speaker] {heard} {'pass' if passed else 'drop'}: {frame.text!r}{saved}")
                    if not passed:
                        continue
                    if not owner and profile is not None:
                        frame = replace(frame, user_id=profile.name if known and profile.name else profile.id)
            yield frame

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        # mlx_whisper loads the weights on the first call; do it now so the user's first sentence isn't the slow one.
        import mlx_whisper

        await asyncio.to_thread(
            mlx_whisper.transcribe, np.zeros(16000, dtype=np.float32), path_or_hf_repo=self._settings.model  # type: ignore[attr-defined]
        )

    def _analyze(self, audio: bytes) -> tuple[np.ndarray | None, float]:
        assert self._embedder is not None and self._voiceprint is not None  # only called with the master check on
        emb = self._embedder.embed(audio)
        return emb, cosine(emb, self._voiceprint)


class JarvisIntentGate(FrameProcessor):
    """
    Pipecat FrameProcessor placed right after local STT (MLX Whisper).
    1. Immediately catches 'Shut up' / 'Be quiet' -> switches Jarvis to BEEP_ONLY mode.
    2. Immediately catches 'You can talk' -> restores VOICE mode and reads queued topics.
    3. When in BEEP_ONLY mode, suppresses all normal conversational responses.
    4. When the master's intent is INTRODUCTION, starts a conversational speaker enrollment once the new speaker
       speaks, asking a couple of questions and linking their stated name ('I'm David') to their voice profile.
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
    ) -> None:
        super().__init__()
        self.state = state
        self.executive_filter = executive_filter
        # What Jarvis said last (the conversation model's last reply), for answers to its own questions.
        self._last_said = last_said
        self._meeting = meeting
        self.registry = registry if registry is not None else SpeakerRegistry(folder=None)
        self._enrollment: _Enrollment | None = None
        self._meeting_log: list[str] = []
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

        # Mute / unmute / introduction act at once and never join a held sentence.
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

    def _start_enrollment(self, hint_name: str | None = None) -> None:
        self._enrollment = _Enrollment(started_at=time.monotonic(), hint_name=hint_name)
        self.state.enrolling = True

    def _stop_enrollment(self) -> None:
        self._enrollment = None
        self.state.enrolling = False

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

    def _buffer_meeting(self, speaker: str, transcript: str) -> None:
        if classify_fast_command(transcript) == UserIntent.IGNORE_BACKGROUND or is_not_for_jarvis(transcript):
            return
        self._meeting_log.append(f"[{speaker}]: {transcript}")
        if len(self._meeting_log) > MEETING_LOG_LIMIT:
            del self._meeting_log[: len(self._meeting_log) - MEETING_LOG_LIMIT]
        logger.info(f"[Meeting] buffered ({speaker}): {transcript!r}")

    def _resolve_profile(self, user_id: str) -> VoiceProfile | None:
        if user_id in ("owner", "u", "", "room"):
            return None
        # None for an id the registry no longer has (merged into a named profile): the sentence counts as unknown.
        return self.registry.get(user_id)

    async def _handle_enrollment(self, profile: VoiceProfile) -> None:
        """Run the conversational enrollment steps once a new speaker starts speaking to Jarvis."""
        assert self._enrollment is not None
        lang_text = LANGUAGES[settings.language]
        if profile.enrolled and profile.name:
            self._stop_enrollment()
            profile.greeted = True
            greeting = lang_text["speaker_greeting"].format(name=profile.name)
            logger.info(f"[Enroll] recognized known speaker {profile.name!r}; greeting by name")
            if self.state.on_speak:
                await self.state.on_speak(greeting)
            return

        if self._enrollment.profile_id is None:
            self._enrollment.profile_id = profile.id
            if self._enrollment.hint_name and not profile.name:
                profile.name = self._enrollment.hint_name
        self._enrollment.started_at = time.monotonic()

        questions = lang_text["enroll_questions"]
        if self._enrollment.questions_asked == 0:
            self._enrollment.questions_asked = 1
            msg = f"{lang_text['enroll_start']} {questions[0]}"
            logger.info(f"[Enroll] started chat with {profile.id} (name={profile.name!r}): {msg!r}")
            if self.state.on_speak:
                await self.state.on_speak(msg)
            return

        if self._enrollment.questions_asked < len(questions):
            q = questions[self._enrollment.questions_asked]
            self._enrollment.questions_asked += 1
            if self._enrollment.questions_asked == len(questions) and not profile.name:
                msg = f"{q} {lang_text['enroll_ask_name']}"
                self._enrollment.asked_name = True
            else:
                msg = q
            logger.info(f"[Enroll] follow-up question for {profile.id} (name={profile.name!r}): {msg!r}")
            if self.state.on_speak:
                await self.state.on_speak(msg)
            return

        if not profile.name:
            self._enrollment.asked_name = True
            msg = lang_text["enroll_ask_name"]
            logger.info(f"[Enroll] asking {profile.id} for their name")
            if self.state.on_speak:
                await self.state.on_speak(msg)
            return

        enrolled = self.registry.link_name(profile.id, profile.name, enroll=True) or profile
        self._stop_enrollment()
        msg = lang_text["enroll_done"].format(name=enrolled.name)
        logger.info(f"[Enroll] completed enrollment for {enrolled.name!r} ({enrolled.id}, clips={enrolled.clips})")
        if self.state.on_speak:
            await self.state.on_speak(msg)

    async def _gate(self, frame: TranscriptionFrame, direction: FrameDirection) -> None:
        transcript = frame.text.strip()
        is_owner = frame.user_id in ("owner", "u", "")
        profile = self._resolve_profile(frame.user_id)
        logger.info(f"[User Transcript] ({self.state.mode.value}): {transcript!r}")

        if self._enrollment is not None and time.monotonic() - self._enrollment.started_at > ENROLL_TIMEOUT_SECS:
            self._stop_enrollment()

        fast = classify_fast_command(transcript)
        # A voice that is neither the master's, enrolled, nor being enrolled never reaches the model. The STT lets
        # it through only while enrolling; this catches the sentence that arrives after the window closed.
        if not is_owner and frame.user_id != "room" and (profile is None or not profile.enrolled) and self._enrollment is None:
            if fast in (UserIntent.MUTE, UserIntent.UNMUTE):
                is_owner = True  # mute and unmute pass from any voice
            else:
                logger.info(f"[Speaker] dropped unknown voice {frame.user_id!r}: {transcript!r}")
                return
        # The same label in both modes, so memory skips a guest's words ([Speaker: David]) like it skips [Room].
        speaker = "User" if is_owner else (f"Speaker: {profile.name}" if profile and profile.name else "Room")

        # While muted only "you can talk" matters; skip the paid LLM call for everything else.
        if self.state.mode == JarvisMode.BEEP_ONLY and fast not in (UserIntent.MUTE, UserIntent.UNMUTE):
            if self._meeting:
                self._buffer_meeting(speaker, transcript)
            return

        # "Not you": Jarvis took room talk as a turn, so it stops answering it. The filter logs the correction.
        if is_not_for_jarvis(transcript) and self.state.on_interrupt:
            await self.state.on_interrupt()

        # A name counts only while enrolling this voice: "It's Friday" from a known guest must not rename them.
        enrolling_this = (
            profile is not None and self._enrollment is not None and self._enrollment.profile_id in (None, profile.id)
        )
        if enrolling_this and fast != UserIntent.IGNORE_BACKGROUND and not is_not_for_jarvis(transcript):
            assert profile is not None and self._enrollment is not None
            stated_name = extract_speaker_name(transcript, allow_bare=self._enrollment.asked_name)
            if stated_name:
                profile = self.registry.link_name(profile.id, stated_name, enroll=False) or profile

        if (
            self._enrollment is not None
            and profile is not None
            and fast not in (UserIntent.MUTE, UserIntent.UNMUTE, UserIntent.IGNORE_BACKGROUND)
            and not is_not_for_jarvis(transcript)
        ):
            await self._handle_enrollment(profile)
            return

        question = self._recent_jarvis_question()
        # In a meeting only the user may answer Jarvis without naming it: the room keeps talking after each reply.
        if (
            self._meeting
            and fast is None
            and not is_not_for_jarvis(transcript)
            and (question is None or not is_owner)
            and not is_meeting_addressed(transcript)
        ):
            self._buffer_meeting(speaker, transcript)
            return

        asked_at = time.monotonic()
        intent = await self.executive_filter.classify_user_utterance(
            transcript=transcript,
            recent_jarvis_question=question,
        )
        # Pipecat's `[Latency]` lines count this inside transcription (the gate re-sends the transcript), so say it here.
        logger.info(f"[Intent] {intent.value} in {(time.monotonic() - asked_at) * 1000:.0f} ms")

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

        if intent == UserIntent.INTRODUCTION:
            if not settings.master_check:
                # Without the master check voices are not told apart, so the conversation model just says hello.
                intent = UserIntent.ADDRESSED_TO_JARVIS
            elif is_owner:
                hint_name = extract_introduced_name(transcript)
                self._start_enrollment(hint_name=hint_name)
                logger.info(f"[Enroll] master introduced a new speaker (hint={hint_name!r}); waiting for their voice.")
                return
            else:
                return

        if intent == UserIntent.IGNORE_BACKGROUND:
            if self._meeting:
                self._buffer_meeting(speaker, transcript)
            else:
                logger.debug(f"Ignored self-talk / unaddressed speech: {transcript!r}")
            return

        self.state.room_turn = not is_owner
        if profile is not None and profile.enrolled and profile.name and not profile.greeted:
            # Once per run; the request itself still goes on to the model below.
            profile.greeted = True
            if self.state.on_speak:
                await self.state.on_speak(LANGUAGES[settings.language]["speaker_greeting"].format(name=profile.name))

        if self._meeting:
            history = "\n".join(self._meeting_log)
            self._meeting_log.clear()
            prefix = f"{MEETING_TRANSCRIPT}\n{history}]\n" if history else ""
            frame = replace(frame, text=f"{prefix}[{speaker}]: {transcript}")
        elif not is_owner:
            frame = replace(frame, text=f"[{speaker}]: {transcript}")

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
