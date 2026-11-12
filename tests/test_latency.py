import unittest
from unittest.mock import patch

from pipecat.observers.user_bot_latency_observer import LatencyBreakdown, LatencyContribution

from jarvis.latency import log_breakdown


def breakdown(user_turn_secs: float | None) -> LatencyBreakdown:
    part = LatencyContribution.model_construct(start_time=0.0, duration_secs=0.4, label="LLM inference")
    return LatencyBreakdown(user_turn_secs=user_turn_secs, contributions=[part], total_secs=0.4)


class TestLogBreakdown(unittest.TestCase):
    def test_an_answered_turn_is_logged(self) -> None:
        with patch("jarvis.latency.logger") as logger, patch.object(
            LatencyBreakdown, "turn_contribution_lines", return_value=["0.400s  LLM inference", "0.400s  TOTAL"]
        ):
            log_breakdown(breakdown(0.2))
        self.assertEqual(
            [c.args[0] for c in logger.info.call_args_list], ["[Latency] 0.400s  LLM inference", "[Latency] 0.400s  TOTAL"]
        )

    def test_speech_jarvis_never_answered_is_not_a_turn(self) -> None:
        # A dropped sentence, then an agent report minutes later: no user turn was released in between.
        with patch("jarvis.latency.logger") as logger:
            log_breakdown(breakdown(None))
        logger.info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
