from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Awaitable


class JarvisMode(str, Enum):
    VOICE = "VOICE"
    BEEP_ONLY = "BEEP_ONLY"


class UserIntent(str, Enum):
    MUTE = "MUTE"
    UNMUTE = "UNMUTE"
    ADDRESSED_TO_JARVIS = "ADDRESSED_TO_JARVIS"
    IGNORE_BACKGROUND = "IGNORE_BACKGROUND"


@dataclass
class AgentTopic:
    """Something an agent said that the user should hear: a question, an approval request, or a finished turn."""

    agent_id: str
    agent_name: str
    summary: str
    # A finished turn's full text: the conversation model retells it instead of reading it out.
    is_report: bool = False


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1][:180] if lines else ""


# Fast deterministic regex patterns for instant mute/unmute without waiting on an LLM call
_MUTE_PATTERNS = re.compile(
    r"\b(shut\s*up|be\s*quiet|stay\s*quiet|mute\s*yourself|stop\s*talking|hush)\b",
    re.IGNORECASE,
)
_UNMUTE_PATTERNS = re.compile(
    r"\b(you\s*can\s*talk|unmute|speak\s*up|talk\s*to\s*me\s*again|jarvis\s*unmute|go\s*ahead\s*and\s*talk)\b",
    re.IGNORECASE,
)


# Whisper writes these on near-silence and noise. Alone they are dropped, so noise can't answer "okay" to an
# approval; a real answer is "yes" or "go ahead".
_WHISPER_FILLERS = {"okay", "ok", "thank you", "thanks", "thank you for watching", "you", "bye"}


def classify_fast_command(transcript: str) -> UserIntent | None:
    """Deterministic zero-latency check for 'shut up' / 'you can talk' commands."""
    cleaned = transcript.strip()
    if not cleaned or cleaned.lower().strip(" .!?,") in _WHISPER_FILLERS:
        return UserIntent.IGNORE_BACKGROUND
    # Unmute first: "don't be quiet, you can talk" holds a mute phrase but means unmute.
    if _UNMUTE_PATTERNS.search(cleaned):
        return UserIntent.UNMUTE
    if _MUTE_PATTERNS.search(cleaned):
        return UserIntent.MUTE
    return None


def synthesize_beep_pcm(
    freq_hz: float = 880.0,
    duration_ms: int = 160,
    sample_rate: int = 16000,
    volume: float = 0.25,
) -> bytes:
    """Generate a clean sine-wave notification beep with fade-in/out envelope (16-bit mono PCM)."""
    num_samples = int(sample_rate * (duration_ms / 1000.0))
    fade_samples = max(1, int(num_samples * 0.15))
    buf = bytearray(num_samples * 2)

    for i in range(num_samples):
        # Smooth envelope to avoid speaker clicks
        if i < fade_samples:
            env = i / fade_samples
        elif i > num_samples - fade_samples:
            env = (num_samples - i) / fade_samples
        else:
            env = 1.0

        sample = math.sin(2.0 * math.pi * freq_hz * (i / sample_rate)) * volume * env
        clamped = max(-32767, min(32767, int(sample * 32767)))
        struct.pack_into("<h", buf, i * 2, clamped)

    return bytes(buf)


class JarvisState:
    """Central state coordinator shared between the Pipecat audio pipeline and Agent Watchers."""

    def __init__(
        self,
        on_speak: Callable[[str], Awaitable[None]] | None = None,
        on_beep: Callable[[bytes], Awaitable[None]] | None = None,
        on_interrupt: Callable[[], Awaitable[None]] | None = None,
        on_report: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.mode: JarvisMode = JarvisMode.VOICE
        self.pending_topics: list[AgentTopic] = []
        self.active_agent_context: dict[str, AgentTopic] = {}
        self.on_speak = on_speak
        self.on_beep = on_beep
        self.on_interrupt = on_interrupt
        self.on_report = on_report

    async def set_mute(self) -> None:
        """Triggered when user says 'Shut up' or 'Be quiet'."""
        self.mode = JarvisMode.BEEP_ONLY
        if self.on_interrupt:
            await self.on_interrupt()
        if self.on_beep:
            # Play a low double-tone to confirm mute
            low_beep = synthesize_beep_pcm(freq_hz=440.0, duration_ms=100)
            await self.on_beep(low_beep)

    async def set_unmute(self) -> str:
        """Triggered when user says 'You can talk'. Speaks queued questions, then retells finished turns."""
        self.mode = JarvisMode.VOICE
        topics, self.pending_topics = self.pending_topics, []
        if not topics:
            msg = "I'm back. Nothing urgent came up while I was quiet."
        else:
            count = len(topics)
            asks = " ".join(f"{t.agent_name} {t.summary}" for t in topics if not t.is_report)
            msg = f"I can talk again. While I was quiet, {count} item{'s' if count > 1 else ''} came up. {asks}".strip()

        if self.on_speak:
            await self.on_speak(msg)
        reports = [t for t in topics if t.is_report]
        if reports:
            await self._retell(reports)
        return msg

    async def notify_topic(self, topic: AgentTopic) -> None:
        """Called by an agent watcher when the agent asks something or finishes a turn."""
        if topic.is_report:
            self.active_agent_context.pop(topic.agent_id, None)  # the turn is over, so is its question
        else:
            self.active_agent_context[topic.agent_id] = topic

        if self.mode == JarvisMode.BEEP_ONLY:
            self.pending_topics.append(topic)
            if self.on_beep:
                beep_pcm = synthesize_beep_pcm(freq_hz=880.0, duration_ms=160)
                await self.on_beep(beep_pcm)
        elif topic.is_report:
            await self._retell([topic])
        elif self.on_speak:
            # The summary starts with the verb ("asks: ...", "needs approval: ..."); the name goes in front of it.
            await self.on_speak(f"{topic.agent_name} {topic.summary}")

    async def _retell(self, reports: list[AgentTopic]) -> None:
        """Finished turns are long markdown; the conversation model shortens them. Offline: last line only."""
        if self.on_report:
            # Trim per report so the agent's name survives, and no queued report disappears behind a long one.
            await self.on_report("\n\n".join(f"{t.agent_name}: {t.summary[-3000:]}" for t in reports))
        elif self.on_speak:
            await self.on_speak(" ".join(f"{t.agent_name}: {_last_line(t.summary)}" for t in reports))
