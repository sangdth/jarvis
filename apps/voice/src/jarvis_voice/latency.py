"""Per-turn latency in the log: how long from the user's last word to Jarvis's first, and where the time went."""

from __future__ import annotations

from loguru import logger
from pipecat.frames.frames import InterruptionFrame
from pipecat.observers.base_observer import FramePushed
from pipecat.observers.user_bot_latency_observer import LatencyBreakdown, UserBotLatencyObserver


class JarvisLatencyObserver(UserBotLatencyObserver):
    """Pipecat's latency observer, for turns that start after the user stopped talking.

    JarvisIntentGate opens each turn only once the sentence passed its checks, and the aggregator interrupts then.
    Pipecat reads that interruption as a cancelled reply and wipes the cycle, so the turn would be timed from
    startup. Only an interruption after the LLM was asked cancels anything.
    """

    async def on_push_frame(self, data: FramePushed) -> None:
        if isinstance(data.frame, InterruptionFrame) and self._llm_request is None:
            return
        await super().on_push_frame(data)


def log_breakdown(breakdown: LatencyBreakdown) -> None:
    """Log one answered turn, one `[Latency]` line per part. Anything else Jarvis says is no turn and is skipped."""
    # No user turn was released in this cycle (the voice or speech check dropped the sentence), so the clock
    # ran from a sentence Jarvis never answered to a later agent report: minutes, not latency.
    if breakdown.user_turn_secs is None:
        return
    for line in breakdown.turn_contribution_lines():
        logger.info(f"[Latency] {line}")


def latency_observer() -> UserBotLatencyObserver:
    """The latency observer, logging each answered turn (needs `enable_metrics` for per-service times)."""
    observer = JarvisLatencyObserver()

    @observer.event_handler("on_latency_breakdown")
    async def _on_breakdown(_observer: UserBotLatencyObserver, breakdown: LatencyBreakdown) -> None:
        log_breakdown(breakdown)

    return observer
