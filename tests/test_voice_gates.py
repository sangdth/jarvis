import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pipecat.frames.frames import (
    ProposedUserStartedSpeakingFrame,
    ProposedUserStoppedSpeakingFrame,
    TextFrame,
    TranscriptionFrame,
)
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
        # The passed sentence is a whole user turn: start (interrupts Jarvis), the words, stop.
        pushed = [c.args[0] for c in self.gate.push_frame.await_args_list]
        self.assertEqual(
            [type(f) for f in pushed],
            [ProposedUserStartedSpeakingFrame, TranscriptionFrame, ProposedUserStoppedSpeakingFrame],
        )
        self.assertIs(pushed[1], frame)

    async def test_background_speech_is_dropped(self) -> None:
        self.filter.classify_user_utterance.return_value = UserIntent.IGNORE_BACKGROUND
        await self.gate.process_frame(heard("honey, dinner?"), FrameDirection.DOWNSTREAM)
        self.gate.push_frame.assert_not_awaited()


class TestSpeakerVerifiedWhisperSTT(unittest.IsolatedAsyncioTestCase):
    """Whisper is mocked; the embedder returns a fixed vector so the score is known."""

    async def run_stt(self, text: str, embedding) -> list:
        import numpy as np
        from pipecat.services.whisper.stt import WhisperSTTServiceMLX

        from jarvis.voice_gates import SpeakerVerifiedWhisperSTT

        async def whisper(_self, _audio):
            yield heard(text)

        embedder = MagicMock()
        embedder.embed.return_value = None if embedding is None else np.array(embedding, dtype=np.float32)
        stt = SpeakerVerifiedWhisperSTT(
            embedder=embedder, voiceprint=np.array([1.0, 0.0], dtype=np.float32), threshold=0.5
        )
        with patch.object(WhisperSTTServiceMLX, "run_stt", new=whisper):
            return [f async for f in stt.run_stt(b"\x00\x00" * 16000)]

    async def test_owner_voice_passes(self) -> None:
        frames = await self.run_stt("build a login page", [0.9, 0.1])
        self.assertEqual([f.text for f in frames], ["build a login page"])

    async def test_other_voice_is_dropped(self) -> None:
        self.assertEqual(await self.run_stt("build a login page", [0.1, 0.9]), [])

    async def test_other_voice_can_still_mute(self) -> None:
        frames = await self.run_stt("shut up", [0.1, 0.9])
        self.assertEqual([f.text for f in frames], ["shut up"])

    async def test_too_short_to_score_is_dropped(self) -> None:
        self.assertEqual(await self.run_stt("build a login page", None), [])


class TestVoiceprint(unittest.TestCase):
    def test_missing_voiceprint_refuses(self) -> None:
        from pathlib import Path

        from jarvis.speaker import load_voiceprint

        with self.assertRaises(FileNotFoundError):
            load_voiceprint(Path("/nonexistent/owner_voice.npy"))

    def test_saved_voiceprint_is_the_average(self) -> None:
        import tempfile
        from pathlib import Path

        import numpy as np

        from jarvis.speaker import load_voiceprint, save_voiceprint

        with tempfile.TemporaryDirectory() as d:
            path = save_voiceprint([np.array([1.0, 0.0]), np.array([0.0, 1.0])], Path(d) / "v.npy")
            np.testing.assert_allclose(load_voiceprint(path), [0.5, 0.5])


if __name__ == "__main__":
    unittest.main()


class TestEchoReference(unittest.IsolatedAsyncioTestCase):
    """Played audio reaches the echo remover at the mic rate, whatever rate it was played at."""

    def setUp(self) -> None:
        from jarvis.voice_gates import EchoFilter, EchoReference

        self.echo = EchoFilter()
        self.echo.aec.feed_reference = MagicMock()
        self.ref = EchoReference(self.echo)

    def test_tts_at_24k_and_beeps_at_16k_both_land_at_16k(self) -> None:
        import numpy as np

        tone = lambda rate: (np.sin(np.arange(rate // 10) * 0.1) * 10000).astype(np.int16).tobytes()  # 100 ms
        out24 = self.ref.to_mic_rate(tone(24000), 24000)
        out16 = self.ref.to_mic_rate(tone(16000), 16000)
        self.assertAlmostEqual(len(out24) / 2, 1600, delta=200)  # 100 ms at 16 kHz; the stream holds back ~8 ms
        self.assertEqual(len(out16) / 2, 1600)

    async def test_played_frames_feed_the_reference_and_pass_on(self) -> None:
        from pipecat.frames.frames import OutputAudioRawFrame

        self.ref.push_frame = AsyncMock()
        patcher = patch.object(FrameProcessor, "process_frame", new=AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)
        frame = OutputAudioRawFrame(audio=b"\x00\x00" * 160, sample_rate=16000, num_channels=1)
        await self.ref.process_frame(frame, FrameDirection.DOWNSTREAM)
        self.echo.aec.feed_reference.assert_called_once_with(frame.audio)
        self.ref.push_frame.assert_awaited_once_with(frame, FrameDirection.DOWNSTREAM)
