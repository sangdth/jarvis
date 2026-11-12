"""Per-turn latency in the log: how long from the user's last word to Jarvis's first, and where the time went."""

from __future__ import annotations

from loguru import logger
from pipecat.observers.user_bot_latency_observer import LatencyBreakdown, UserBotLatencyObserver


def log_breakdown(breakdown: LatencyBreakdown) -> None:
    """Log one answered turn, one `[Latency]` line per part. Anything else Jarvis says is no turn and is skipped."""
    # No user turn was released in this cycle (the voice or speech check dropped the sentence), so the clock
    # ran from a sentence Jarvis never answered to a later agent report: minutes, not latency.
    if breakdown.user_turn_secs is None:
        return
    for line in breakdown.turn_contribution_lines():
        logger.info(f"[Latency] {line}")


def latency_observer() -> UserBotLatencyObserver:
    """Pipecat's latency observer, logging each answered turn (needs `enable_metrics` for per-service times)."""
    observer = UserBotLatencyObserver()

    @observer.event_handler("on_latency_breakdown")
    async def _on_breakdown(_observer: UserBotLatencyObserver, breakdown: LatencyBreakdown) -> None:
        log_breakdown(breakdown)

    return observer
