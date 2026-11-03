import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pipecat.frames.frames import TranscriptionFrame, TextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from jarvis.state import AgentTopic, JarvisMode, JarvisState, UserIntent
from jarvis.voice_gates import JarvisIntentGate


def heard(text: str) -> TranscriptionFrame:
    return TranscriptionFrame(text=text, user_id="u", timestamp="0")


class TestIntentGate(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.state = JarvisState()
        self.filter = MagicMock()
        self.filter.classify_user_utterance = AsyncMock(return_value=UserIntent.ADDRESSED_TO_JARVIS)
        self.gate = JarvisIntentGate(self.state, self.filter)
        self.gate.push_frame = AsyncMock()
        patcher = patch.object(FrameProcessor, "process_frame", new=AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_other_frames_pass_through(self) -> None:
        frame = TextFrame(text="hi")
        await self.gate.process_frame(frame, FrameDirection.DOWNSTREAM)
        self.gate.push_frame.assert_awaited_once_with(frame, FrameDirection.DOWNSTREAM)
        self.filter.classify_user_utterance.assert_not_awaited()

    async def test_muted_skips_the_paid_check(self) -> None:
        self.state.mode = JarvisMode.BEEP_ONLY
        await self.gate.process_frame(heard("Build a login page"), FrameDirection.DOWNSTREAM)
        self.filter.classify_user_utterance.assert_not_awaited()
        self.gate.push_frame.assert_not_awaited()

    async def test_open_question_goes_to_the_check_and_speech_passes_on(self) -> None:
        self.state.active_agent_context["hermes"] = AgentTopic("hermes", "Hermes", "needs approval: rm x")
        frame = heard("yes go ahead")
        await self.gate.process_frame(frame, FrameDirection.DOWNSTREAM)
        self.filter.classify_user_utterance.assert_awaited_once_with(
            transcript="yes go ahead", recent_jarvis_question="needs approval: rm x"
        )
        self.gate.push_frame.assert_awaited_once_with(frame, FrameDirection.DOWNSTREAM)

    async def test_background_speech_is_dropped(self) -> None:
        self.filter.classify_user_utterance.return_value = UserIntent.IGNORE_BACKGROUND
        await self.gate.process_frame(heard("honey, dinner?"), FrameDirection.DOWNSTREAM)
        self.gate.push_frame.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
