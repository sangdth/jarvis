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
EVENT_HEARTBEAT = "heartbeat"

_MAX_PREVIEW_CHARS = 280


def preview(text: str | None, limit: int = _MAX_PREVIEW_CHARS) -> str:
    """Collapses whitespace and truncates a string for compact telemetry payloads."""
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 1]}…"


class TelemetryHub:
    """Bounded in-memory event ring buffer with fan-out to SSE subscribers."""

    def __init__(self, max_events: int = 500, queue_size: int = 1000) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
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
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:
                # A stalled dashboard must never back-pressure the reconciler.
                continue
        return envelope

    def snapshot(self, limit: int = 100) -> list[dict[str, Any]]:
        """Returns the most recent `limit` events, oldest first."""
        if limit <= 0:
            return []
        events = list(self._events)
        return events[-limit:]

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def heartbeat(self, **fields: Any) -> dict[str, Any]:
        return self.emit(
            EVENT_HEARTBEAT,
            uptime_s=round(self.uptime_seconds, 1),
            subscribers=self.subscriber_count,
            **fields,
        )


__all__ = [
    "EVENT_APPROVAL_REQUEST",
    "EVENT_APPROVAL_RESPONDED",
    "EVENT_ATTEMPT_FINISHED",
    "EVENT_BOARD_RPC",
    "EVENT_HEARTBEAT",
    "EVENT_HTTP_COMPLETED",
    "EVENT_HTTP_INBOUND",
    "EVENT_PI_PROGRESS",
    "EVENT_RECONCILER_PHASE",
    "EVENT_RUN_STATUS",
    "TelemetryHub",
    "preview",
]
