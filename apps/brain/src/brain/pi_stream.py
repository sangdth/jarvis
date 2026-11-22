"""Reading `pi --mode json` events: the assistant's own words and what the turn has cost so far.

Shared by the board's workers and the one-shot answer runner, so both count streaming and finished usage the
same way instead of drifting apart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


def extract_event_cost(event: dict[str, Any]) -> float:
    """Extracts incremental or total USD cost from a `pi --mode json` event if present."""
    if isinstance(event.get("cost_usd"), (int, float)):
        return float(event["cost_usd"])
    usage = event.get("usage")
    if isinstance(usage, dict):
        if isinstance(usage.get("cost_usd"), (int, float)):
            return float(usage["cost_usd"])
        cost = usage.get("cost")
        if isinstance(cost, (int, float)):
            return float(cost)
        if isinstance(cost, dict) and isinstance(cost.get("total"), (int, float)):
            return float(cost["total"])
    msg = event.get("message")
    if isinstance(msg, dict):
        return extract_event_cost(msg)
    return 0.0


def extract_assistant_text(event: dict[str, Any]) -> str | None:
    """Extracts assistant text from a `pi --mode json` event."""
    msg = event.get("message") if isinstance(event.get("message"), dict) else event
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return None
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(item.get("text", "")).strip()
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
        ]
        joined = "\n".join(p for p in parts if p)
        return joined if joined else None
    return None


@dataclass
class PiStreamState:
    """One `pi` process as it runs: its session id, its latest words, and its spend so far.

    A `message_update` carries the running total for the message being streamed, so it replaces the streaming
    figure instead of adding to it; a finished event adds its own cost once.
    """

    session_id: str | None = None
    last_text: str = ""
    completed_cost: float = 0.0
    streaming_cost: float = 0.0

    @property
    def total_cost(self) -> float:
        return self.completed_cost + self.streaming_cost

    def feed_line(self, line_str: str) -> None:
        if not line_str:
            return
        try:
            event = json.loads(line_str)
        except json.JSONDecodeError:
            self.last_text = line_str
            return
        if not isinstance(event, dict):
            return
        if event.get("type") == "session" and isinstance(event.get("id"), str):
            self.session_id = event["id"]
        elif isinstance(event.get("session_id"), str):
            self.session_id = event["session_id"]
        elif isinstance(event.get("sessionId"), str):
            self.session_id = event["sessionId"]
        text = extract_assistant_text(event)
        if text:
            self.last_text = text
        ev_cost = extract_event_cost(event)
        if ev_cost > 0:
            if event.get("type") == "message_update":
                self.streaming_cost = max(self.streaming_cost, ev_cost)
            else:
                self.completed_cost += ev_cost
                self.streaming_cost = 0.0
