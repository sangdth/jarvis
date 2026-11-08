"""Speech-check decisions, and the corrections the user's next move implies, for retraining Laya's jarvis-intent.

One JSONL line per heard sentence in JARVIS_HOME/feedback/intent.jsonl; corrections are later lines that point back
at a decision by id. The file is append-only and stays on this Mac; the laya repo's `distill.py feedback` reads it.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loguru import logger

from jarvis.config import JARVIS_HOME
from jarvis.state import UserIntent

FEEDBACK_FILE = JARVIS_HOME / "feedback" / "intent.jsonl"
# A model mute undone this fast was not a mute (and the other way round).
UNDO_SECS = 30.0
# "Not you" this soon after Jarvis took a sentence: that sentence was room talk.
NOT_YOU_SECS = 20.0
# An ignored sentence said again this soon, as an addressed one: it was meant for Jarvis.
REPEAT_SECS = 15.0
REPEAT_OVERLAP = 0.6
# Only model answers can be wrong in a way worth training on; fixed phrases are rules.
MODEL_SOURCES = {"laya", "jev"}


@dataclass(frozen=True)
class _Seen:
    id: str
    at: float
    intent: UserIntent
    source: str
    text: str


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def overlap(old: str, new: str) -> float:
    """Share of the old sentence's words found in the new one."""
    words = _words(old)
    return len(words & _words(new)) / len(words) if words else 0.0


class FeedbackLog:
    def __init__(self, path: Path = FEEDBACK_FILE, clock: Callable[[], float] = time.monotonic) -> None:
        self.path = path
        self._clock = clock
        self._recent: deque[_Seen] = deque(maxlen=20)

    def decision(
        self,
        transcript: str,
        state: dict | None,
        intent: UserIntent,
        source: str,
        laya: dict | None = None,
        jev: dict | None = None,
    ) -> str:
        """Log one decision (source: fast, not-you, laya, jev or none) and any correction it implies; returns its id."""
        seen = _Seen(uuid.uuid4().hex[:12], self._clock(), intent, source, transcript)
        self._write(
            {
                "id": seen.id,
                "at": datetime.now().isoformat(timespec="seconds"),
                "state": state or {"document": transcript},
                "acted": {"intent": intent.value, "source": source},
                "laya": laya,
                "jev": jev,
            }
        )
        self._check(seen)
        self._recent.append(seen)
        return seen.id

    def late(self, decision_id: str, jev: dict) -> None:
        """Jev's answer when it arrived after Jarvis already acted on Laya's."""
        self._write({"late_for": decision_id, "jev": jev})

    def _last(self, intent: UserIntent) -> _Seen | None:
        return next((s for s in reversed(self._recent) if s.intent == intent), None)

    def _check(self, new: _Seen) -> None:
        if new.intent in (UserIntent.MUTE, UserIntent.UNMUTE):
            # The newest opposite command decides: a "shut up" between a model mute and the unmute vouches for it.
            undone = UserIntent.UNMUTE if new.intent == UserIntent.MUTE else UserIntent.MUTE
            old = self._last(undone)
            if old and old.source in MODEL_SOURCES and new.at - old.at <= UNDO_SECS:
                self._correct(old, f"{new.intent.value.lower()}d within {UNDO_SECS:.0f}s", "strong", not_label=undone)
        elif new.source == "not-you":
            old = self._last(UserIntent.ADDRESSED_TO_JARVIS)
            if old and old.source in MODEL_SOURCES and new.at - old.at <= NOT_YOU_SECS:
                self._correct(old, "not you", "strong", label=UserIntent.IGNORE_BACKGROUND)
        elif new.intent == UserIntent.ADDRESSED_TO_JARVIS:
            old = self._last(UserIntent.IGNORE_BACKGROUND)
            if (
                old
                and old.source in MODEL_SOURCES
                and new.at - old.at <= REPEAT_SECS
                and overlap(old.text, new.text) >= REPEAT_OVERLAP
            ):
                self._correct(old, "said again", "medium", label=UserIntent.ADDRESSED_TO_JARVIS)

    def _correct(
        self,
        old: _Seen,
        signal: str,
        strength: str,
        label: UserIntent | None = None,
        not_label: UserIntent | None = None,
    ) -> None:
        logger.info(f"[Feedback] {signal}: {old.text!r} was not {old.intent.value}")
        record: dict[str, str | None] = {"correction_of": old.id, "signal": signal, "strength": strength}
        record |= {"label": label.value} if label else {"not_label": not_label.value if not_label else None}
        self._write(record)
        # One correction per decision: drop it so a later move cannot correct it again.
        self._recent = deque((s for s in self._recent if s.id != old.id), maxlen=self._recent.maxlen)

    def _write(self, record: dict) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning(f"Could not write speech-check feedback to {self.path} ({exc!r}).")
