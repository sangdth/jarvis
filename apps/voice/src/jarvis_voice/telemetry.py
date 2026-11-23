"""The voice side of the brain's telemetry bus: one line per stage of a turn, for `jarvis top`.

The supervisor hands this module the brain's `TelemetryHub.emit`, so the cockpit sees Whisper, the voice check,
the cleanup, memory, the conversation model's reasoning and the reply on the same SSE stream as the board. A
standalone `jarvis` with no server leaves it unbound and every call is a no-op.

Text is capped: a long prompt would otherwise flood the queue a subscribed cockpit has to drain. The full text
stays in the log and in the session files.
"""

from __future__ import annotations

import itertools
import time
from typing import Any, Callable

from loguru import logger

# One payload preview. Full prompts and agent output live in the log and on disk, not on the event stream.
MAX_TEXT = 2000

_emit: Callable[..., Any] | None = None
_turn_ids = itertools.count(1)
_current: str | None = None


def bind(emit: Callable[..., Any] | None) -> None:
    """Attaches the brain's hub; `None` (standalone) turns every stage into a no-op."""
    global _emit
    _emit = emit


def new_turn() -> str:
    """Starts a turn: every stage after it until the next one lands under this id in the cockpit."""
    global _current
    _current = f"t{next(_turn_ids)}"
    return _current


def current_turn() -> str:
    """The turn in progress, or a fresh one when a stage arrives without a sentence before it."""
    return _current or new_turn()


def emit(kind: str, **fields: Any) -> None:
    """Publishes one stage. Never raises: a broken cockpit must not break a conversation."""
    if _emit is None:
        return
    try:
        # `ts` is the voice's own clock, which is the same Mac as the hub's; the hub keeps its own too.
        turn = (_current or "") if kind == "voice.level" else current_turn()
        _emit(kind, turn_id=turn, ts=time.time(), **_clip(fields))
    except Exception as exc:  # noqa: BLE001 - telemetry is best effort by definition
        logger.debug(f"[Telemetry] dropped {kind}: {exc!r}")


def preview(text: Any, limit: int = MAX_TEXT) -> str:
    """One compact line of a payload, for events that only need a glance."""
    collapsed = " ".join(str(text or "").split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[: limit - 1]}…"


def _clip(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        out[key] = preview(value) if isinstance(value, str) and len(value) > MAX_TEXT else value
    return out


__all__ = ["bind", "current_turn", "emit", "new_turn", "preview", "MAX_TEXT"]
