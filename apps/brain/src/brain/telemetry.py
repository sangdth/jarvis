"""
Live telemetry bus for the Brain dashboard (`brain top`).

`TelemetryHub` is a dependency-free, in-memory ring buffer plus fan-out bus. The daemon publishes
transient events here (HTTP inbound, reconciler phases, `board.ts` RPC calls, `pi` progress) that are
useful *while they happen* but do not need to be written to `state.db`. The Ratatui dashboard
subscribes over `GET /v1/telemetry/events` (SSE) and reads durable history straight from SQLite.

Nothing in this module touches disk, so publishing is cheap and can never fail a worker run.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

# Event kinds emitted by the daemon. Keep in sync with `tui/src/model.rs`.
EVENT_HTTP_INBOUND = "http.inbound"
EVENT_HTTP_COMPLETED = "http.completed"
EVENT_RUN_STATUS = "run.status"
EVENT_APPROVAL_REQUEST = "approval.request"
EVENT_APPROVAL_RESPONDED = "approval.responded"
EVENT_RECONCILER_PHASE = "reconciler.phase"
EVENT_BOARD_RPC = "board.rpc"
EVENT_PI_PROGRESS = "pi.progress"
EVENT_ATTEMPT_FINISHED = "attempt.finished"
EVENT_PAIR_REQUESTED = "pair.requested"
EVENT_PAIR_RESOLVED = "pair.resolved"
EVENT_HEARTBEAT = "heartbeat"

# The voice's turn, stage by stage (`jarvis_voice/telemetry.py`): what the user said, how it was checked,
# what the model was given and thought, which tool it called, and what came back to the speaker.
EVENT_VOICE_EAR = "voice.ear"
EVENT_VOICE_STT = "voice.stt"
EVENT_VOICE_INTENT = "voice.intent"
EVENT_VOICE_CLEANUP = "voice.cleanup"
EVENT_VOICE_FASTCMD = "voice.fastcmd"
EVENT_VOICE_MEMORY = "voice.memory"
EVENT_VOICE_REQUEST = "voice.request"
EVENT_VOICE_THINKING = "voice.thinking"
EVENT_VOICE_TOOL = "voice.tool"
EVENT_VOICE_LATENCY = "voice.latency"
EVENT_VOICE_DISPATCH = "voice.dispatch"
EVENT_VOICE_RETURN = "voice.return"
EVENT_VOICE_RETELL = "voice.retell"
EVENT_VOICE_TTS = "voice.tts"
EVENT_VOICE_MODE = "voice.mode"
EVENT_BRAIN_ROUTE = "brain.route"

_MAX_PREVIEW_CHARS = 280


def preview(text: str | None, limit: int = _MAX_PREVIEW_CHARS) -> str:
    """Collapses whitespace and truncates a string for compact telemetry payloads."""
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 1]}…"


_STICKY_EVENTS = frozenset({EVENT_RECONCILER_PHASE, EVENT_VOICE_EAR, EVENT_VOICE_MODE})


class TelemetryHub:
    """Bounded in-memory event ring buffer with fan-out to SSE subscribers."""

    def __init__(self, max_events: int = 2000, queue_size: int = 1000) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._sticky: dict[str, dict[str, Any]] = {}
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._queue_size = queue_size
        self._seq = 0
        self._started_monotonic = time.monotonic()

    @property
    def sequence(self) -> int:
        return self._seq

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self._started_monotonic

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _fanout(self, envelope: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:
                # A stalled dashboard must never back-pressure the reconciler.
                continue

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        """Publishes one event, returning the enriched envelope (sequence + timestamp included)."""
        self._seq += 1
        envelope: dict[str, Any] = {
            "seq": self._seq,
            "ts": time.time(),
            "event": event,
            **fields,
        }
        self._events.append(envelope)
        if event in _STICKY_EVENTS:
            self._sticky[event] = envelope
        self._fanout(envelope)
        return envelope

    def snapshot(self, limit: int = 100) -> list[dict[str, Any]]:
        """Returns the most recent `limit` events, oldest first, keeping sticky state events."""
        if limit <= 0:
            return []
        tail = list(self._events)[-limit:]
        seen_seqs = {e["seq"] for e in tail}
        pinned = sorted((e for e in self._sticky.values() if e["seq"] not in seen_seqs), key=lambda e: e["seq"])
        return pinned + tail

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def heartbeat(self, **fields: Any) -> dict[str, Any]:
        """Keepalive for live SSE subscribers; kept out of the ring buffer so idle time never evicts turns."""
        self._seq += 1
        envelope: dict[str, Any] = {
            "seq": self._seq,
            "ts": time.time(),
            "event": EVENT_HEARTBEAT,
            "uptime_s": round(self.uptime_seconds, 1),
            "subscribers": self.subscriber_count,
            **fields,
        }
        self._fanout(envelope)
        return envelope


__all__ = [
    "EVENT_APPROVAL_REQUEST",
    "EVENT_APPROVAL_RESPONDED",
    "EVENT_ATTEMPT_FINISHED",
    "EVENT_BOARD_RPC",
    "EVENT_BRAIN_ROUTE",
    "EVENT_HEARTBEAT",
    "EVENT_HTTP_COMPLETED",
    "EVENT_HTTP_INBOUND",
    "EVENT_PAIR_REQUESTED",
    "EVENT_PAIR_RESOLVED",
    "EVENT_PI_PROGRESS",
    "EVENT_RECONCILER_PHASE",
    "EVENT_RUN_STATUS",
    "EVENT_VOICE_CLEANUP",
    "EVENT_VOICE_DISPATCH",
    "EVENT_VOICE_EAR",
    "EVENT_VOICE_FASTCMD",
    "EVENT_VOICE_INTENT",
    "EVENT_VOICE_LATENCY",
    "EVENT_VOICE_MEMORY",
    "EVENT_VOICE_MODE",
    "EVENT_VOICE_REQUEST",
    "EVENT_VOICE_RETELL",
    "EVENT_VOICE_RETURN",
    "EVENT_VOICE_STT",
    "EVENT_VOICE_THINKING",
    "EVENT_VOICE_TOOL",
    "EVENT_VOICE_TTS",
    "TelemetryHub",
    "preview",
]
