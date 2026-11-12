import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import FramePushed
from pipecat.observers.user_bot_latency_observer import LatencyBreakdown
from pipecat.processors.frame_processor import FrameDirection

from jarvis.latency import JarvisLatencyObserver, log_breakdown


class TestJarvisLatencyObserver(unittest.IsolatedAsyncioTestCase):
    """Real frames in the order Jarvis's pipeline pushes them, on a fake clock."""

    async def asyncSetUp(self) -> None:
        self.now = 1000.0
        self.observer = JarvisLatencyObserver(time_source=lambda: self.now)
        self.breakdowns: list[LatencyBreakdown] = []

        @self.observer.event_handler("on_latency_breakdown")
        async def collect(_observer, breakdown: LatencyBreakdown) -> None:
            self.breakdowns.append(breakdown)

        await self.observer.on_pipeline_started()
        self.now += 3600  # an hour of uptime: a turn timed from startup would show it

    async def push(self, frame, source: str = "x", after: float = 0.0) -> None:
        self.now += after
        await self.observer.on_push_frame(
            FramePushed(
                source=SimpleNamespace(name=source),  # type: ignore[arg-type]
                destination=SimpleNamespace(name="y"),  # type: ignore[arg-type]
                frame=frame,
                direction=FrameDirection.DOWNSTREAM,
                timestamp=0,
            )
        )

    async def test_a_turn_opened_by_the_gate_is_timed_from_the_users_silence(self) -> None:
        await self.push(VADUserStartedSpeakingFrame())
        await self.push(VADUserStoppedSpeakingFrame(stop_secs=0.2, timestamp=self.now + 1.0), after=1.0)
        await self.push(TranscriptionFrame(text="hi", user_id="", timestamp="0"), "stt", after=0.3)
        await self.push(InterruptionFrame(), "aggregator", after=0.1)  # the gate's proposed turn start
        await self.push(TranscriptionFrame(text="hi", user_id="", timestamp="0"), "gate")
        await self.push(UserStoppedSpeakingFrame(), "aggregator")
        await self.push(LLMFullResponseStartFrame(), "llm", after=0.01)
        await self.push(LLMTextFrame("hello"), "llm", after=0.5)
        await self.push(TTSAudioRawFrame(b"", 16000, 1), "tts", after=0.2)
        await self.push(BotStartedSpeakingFrame(), "out", after=0.05)
        for _ in range(5):  # Pipecat runs event handlers as tasks
            await asyncio.sleep(0)

        (breakdown,) = self.breakdowns
        self.assertEqual(breakdown.measured_from, "user_silence")
        self.assertLess(breakdown.total_secs, 2)
        self.assertIsNotNone(breakdown.user_turn_secs)

    async def test_an_interruption_after_the_llm_was_asked_still_cancels(self) -> None:
        await self.push(VADUserStartedSpeakingFrame())
        await self.push(VADUserStoppedSpeakingFrame(stop_secs=0.2, timestamp=self.now + 1.0), after=1.0)
        await self.push(LLMFullResponseStartFrame(), "llm", after=0.3)
        await self.push(InterruptionFrame(), "aggregator")
        self.assertIsNone(self.observer._llm_request)  # the cancelled reply's moments are gone


class TestLogBreakdown(unittest.TestCase):
    def test_speech_jarvis_never_answered_is_not_a_turn(self) -> None:
        # A dropped sentence, then an agent report minutes later: no user turn was released in between.
        with patch("jarvis.latency.logger") as logger:
            log_breakdown(LatencyBreakdown(user_turn_secs=None))
        logger.info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
