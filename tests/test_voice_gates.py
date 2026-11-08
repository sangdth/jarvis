import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    ProposedUserStartedSpeakingFrame,
    ProposedUserStoppedSpeakingFrame,
    TextFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from jarvis.state import AgentTopic, JarvisMode, JarvisState, UserIntent
from jarvis.voice_gates import JarvisIntentGate, last_assistant_text


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

    def turns(self) -> list[str]:
        return [
            c.args[0].text for c in self.gate.push_frame.await_args_list if isinstance(c.args[0], TranscriptionFrame)
        ]

    @patch("jarvis.voice_gates.HOLD_SECS", 0.05)
    async def test_trailing_off_waits_and_joins_the_rest(self) -> None:
        down = FrameDirection.DOWNSTREAM
        await self.gate.process_frame(heard(" Open the... "), down)
        await self.gate.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.1)  # longer than the hold: speaking again keeps it open
        await self.gate.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        await self.gate.process_frame(heard(" folder in Happy Voxel. "), down)
        await asyncio.sleep(0.1)
        self.assertEqual(self.turns(), ["Open the... folder in Happy Voxel."])
        self.filter.classify_user_utterance.assert_awaited_once()

    @patch("jarvis.voice_gates.HOLD_SECS", 0.05)
    async def test_trailing_off_alone_goes_on_after_the_hold(self) -> None:
        frame = heard("Hmm...")
        await self.gate.process_frame(frame, FrameDirection.DOWNSTREAM)
        self.gate.push_frame.assert_not_awaited()
        await asyncio.sleep(0.1)
        self.assertEqual(self.turns(), ["Hmm..."])

    async def test_mute_does_not_wait_for_a_held_sentence(self) -> None:
        self.filter.classify_user_utterance.return_value = UserIntent.MUTE
        await self.gate.process_frame(heard("Open the..."), FrameDirection.DOWNSTREAM)
        await self.gate.process_frame(heard("shut up"), FrameDirection.DOWNSTREAM)
        self.filter.classify_user_utterance.assert_awaited_once_with(transcript="shut up", recent_jarvis_question=None)
        await self.gate.cleanup()

    @patch("jarvis.voice_gates.HOLD_SECS", 0.05)
    async def test_a_joined_sentence_is_not_held_again(self) -> None:
        down = FrameDirection.DOWNSTREAM
        await self.gate.process_frame(heard("Yes, you should have got me how the..."), down)
        await self.gate.process_frame(heard("The tree is walking under the..."), down)
        self.assertEqual(self.turns(), ["Yes, you should have got me how the... The tree is walking under the..."])

    async def test_answer_right_after_jarvis_speaks_carries_its_reply(self) -> None:
        self.gate = JarvisIntentGate(self.state, self.filter, last_said=lambda: "Which project, Opscom or another?")
        self.gate.push_frame = AsyncMock()
        await self.gate.process_frame(heard("the tree to continue walking."), FrameDirection.DOWNSTREAM)
        self.filter.classify_user_utterance.assert_awaited_with(
            transcript="the tree to continue walking.", recent_jarvis_question=None
        )  # Jarvis has not spoken yet: no context
        await self.gate.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        await self.gate.process_frame(heard("the tree to continue walking."), FrameDirection.DOWNSTREAM)
        self.filter.classify_user_utterance.assert_awaited_with(
            transcript="the tree to continue walking.", recent_jarvis_question="Which project, Opscom or another?"
        )

    async def test_jarvis_reply_expires_after_the_window(self) -> None:
        self.gate = JarvisIntentGate(self.state, self.filter, last_said=lambda: "Done.")
        await self.gate.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        with patch("jarvis.voice_gates.REPLY_WINDOW_SECS", 0.0):
            await self.gate.process_frame(heard("honey, dinner?"), FrameDirection.DOWNSTREAM)
        self.filter.classify_user_utterance.assert_awaited_with(
            transcript="honey, dinner?", recent_jarvis_question=None
        )

    async def test_agent_question_wins_over_jarvis_reply(self) -> None:
        self.gate = JarvisIntentGate(self.state, self.filter, last_said=lambda: "Anything else?")
        self.gate.push_frame = AsyncMock()
        self.state.active_agent_context["hermes"] = AgentTopic("hermes", "Hermes", "needs approval: rm x")
        await self.gate.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        await self.gate.process_frame(heard("yes"), FrameDirection.DOWNSTREAM)
        self.filter.classify_user_utterance.assert_awaited_with(
            transcript="yes", recent_jarvis_question="needs approval: rm x"
        )

    async def test_background_speech_is_dropped(self) -> None:
        self.filter.classify_user_utterance.return_value = UserIntent.IGNORE_BACKGROUND
        await self.gate.process_frame(heard("honey, dinner?"), FrameDirection.DOWNSTREAM)
        self.gate.push_frame.assert_not_awaited()

    async def test_not_you_stops_jarvis_talking(self) -> None:
        self.state.on_interrupt = AsyncMock()
        self.filter.classify_user_utterance.return_value = UserIntent.IGNORE_BACKGROUND
        await self.gate.process_frame(heard("Not you, Jarvis"), FrameDirection.DOWNSTREAM)
        self.state.on_interrupt.assert_awaited_once()
        self.filter.classify_user_utterance.assert_awaited_once()  # the filter logs the correction
        self.gate.push_frame.assert_not_awaited()

    async def test_meeting_mode_buffers_room_talk_until_jarvis_is_named(self) -> None:
        gate = JarvisIntentGate(self.state, self.filter, meeting=True)
        gate.push_frame = AsyncMock()
        down = FrameDirection.DOWNSTREAM

        await gate.process_frame(TranscriptionFrame(text="Let's ship the API on Tuesday.", user_id="room", timestamp="0"), down)
        await gate.process_frame(TranscriptionFrame(text="Agreed, I'll update the docs.", user_id="owner", timestamp="0"), down)
        self.filter.classify_user_utterance.assert_not_awaited()
        gate.push_frame.assert_not_awaited()

        await gate.process_frame(
            TranscriptionFrame(text="Jarvis, put that deadline on the calendar.", user_id="owner", timestamp="0"), down
        )
        self.filter.classify_user_utterance.assert_awaited_once()
        pushed = [c.args[0] for c in gate.push_frame.await_args_list if isinstance(c.args[0], TranscriptionFrame)]
        self.assertEqual(
            [f.text for f in pushed],
            [
                "[Meeting transcript:\n"
                "[Room]: Let's ship the API on Tuesday.\n"
                "[User]: Agreed, I'll update the docs.]\n"
                "[User]: Jarvis, put that deadline on the calendar."
            ],
        )

    async def test_meeting_mode_hears_direct_reply_to_jarvis_without_wake_word(self) -> None:
        gate = JarvisIntentGate(self.state, self.filter, last_said=lambda: "Which day should I book?", meeting=True)
        gate.push_frame = AsyncMock()
        await gate.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        reply = TranscriptionFrame(text="Tuesday afternoon", user_id="room", timestamp="0")
        await gate.process_frame(reply, FrameDirection.DOWNSTREAM)
        self.filter.classify_user_utterance.assert_awaited_once_with(
            transcript="Tuesday afternoon", recent_jarvis_question="Which day should I book?"
        )
        pushed = [c.args[0] for c in gate.push_frame.await_args_list if isinstance(c.args[0], TranscriptionFrame)]
        self.assertEqual([f.text for f in pushed], ["[Room]: Tuesday afternoon"])


class TestSpeakerVerifiedWhisperSTT(unittest.IsolatedAsyncioTestCase):
    """Whisper is mocked; the embedder returns a fixed vector so the score is known."""

    def setUp(self) -> None:
        from jarvis.config import settings

        # Clip saving stays off unless a test turns it on: the developer's own .env may set SAVE_SEGMENTS.
        saving = patch.object(settings, "save_segments", 0)
        saving.start()
        self.addCleanup(saving.stop)

    async def run_stt(self, text: str | None, embedding, *, meeting: bool = False) -> list:
        import numpy as np
        from pipecat.services.whisper.stt import WhisperSTTServiceMLX

        from jarvis.voice_gates import SpeakerVerifiedWhisperSTT

        async def whisper(_self, _audio):
            if text is not None:  # None: Whisper dropped the segment (no_speech_prob), so no frame at all
                yield heard(text)

        embedder = MagicMock()
        embedder.embed.return_value = None if embedding is None else np.array(embedding, dtype=np.float32)
        stt = SpeakerVerifiedWhisperSTT(
            embedder=embedder, voiceprint=np.array([1.0, 0.0], dtype=np.float32), threshold=0.5, meeting=meeting
        )
        with patch.object(WhisperSTTServiceMLX, "run_stt", new=whisper):
            return [f async for f in stt.run_stt(b"\x00\x00" * 16000)]

    async def test_owner_voice_passes(self) -> None:
        frames = await self.run_stt("build a login page", [0.9, 0.1])
        self.assertEqual([f.text for f in frames], ["build a login page"])

    async def test_other_voice_is_dropped(self) -> None:
        self.assertEqual(await self.run_stt("build a login page", [0.1, 0.9]), [])

    async def test_other_voice_passes_as_room_in_meeting_mode(self) -> None:
        room = await self.run_stt("what about the deadline", [0.1, 0.9], meeting=True)
        owner = await self.run_stt("let's ask Jarvis", [0.9, 0.1], meeting=True)
        self.assertEqual([(f.user_id, f.text) for f in room], [("room", "what about the deadline")])
        self.assertEqual([(f.user_id, f.text) for f in owner], [("owner", "let's ask Jarvis")])

    async def test_other_voice_can_still_mute(self) -> None:
        frames = await self.run_stt("shut up", [0.1, 0.9])
        self.assertEqual([f.text for f in frames], ["shut up"])

    async def test_too_short_to_score_is_dropped(self) -> None:
        self.assertEqual(await self.run_stt("build a login page", None), [])

    async def test_saving_keeps_dropped_sentences_too(self) -> None:
        with patch("jarvis.voice_gates.settings") as settings, patch("jarvis.voice_gates.save_clip") as save:
            settings.save_segments = 5
            save.return_value = None
            await self.run_stt("build a login page", [0.1, 0.9])  # other voice: dropped
            await self.run_stt(None, [0.9, 0.1])  # Whisper heard nothing
        self.assertEqual(save.call_count, 2)
        self.assertEqual(save.call_args.args[0], b"\x00\x00" * 16000)
        self.assertEqual(save.call_args.args[2], 5)

    async def test_saving_is_off_by_default(self) -> None:
        with patch("jarvis.voice_gates.settings") as settings, patch("jarvis.voice_gates.save_clip") as save:
            settings.save_segments = 0
            await self.run_stt("build a login page", [0.9, 0.1])
        save.assert_not_called()


class TestSaveClip(unittest.TestCase):
    def test_writes_a_private_wav_and_keeps_the_newest(self) -> None:
        import os
        import stat
        import tempfile
        import wave
        from pathlib import Path

        from jarvis.voice_gates import save_clip

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "clips"
            paths = [save_clip(b"\x01\x00" * 1600, 16000, keep=2, folder=folder) for _ in range(3)]
            self.assertEqual(sorted(folder.glob("*.wav")), paths[1:])  # the oldest was deleted
            with wave.open(str(paths[-1])) as clip:
                self.assertEqual((clip.getframerate(), clip.getnchannels(), clip.getnframes()), (16000, 1, 1600))
            self.assertEqual(stat.S_IMODE(os.stat(paths[-1]).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)

    def test_a_disk_error_does_not_raise(self) -> None:
        from pathlib import Path

        from jarvis.voice_gates import save_clip

        self.assertIsNone(save_clip(b"\x00\x00", 16000, keep=1, folder=Path("/dev/null/clips")))


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


class TestLastAssistantText(unittest.TestCase):
    def test_newest_assistant_text(self) -> None:
        messages = [
            {"role": "assistant", "content": "old"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "Which project?"}]},
            {"role": "system", "content": "report"},
        ]
        self.assertEqual(last_assistant_text(messages), "Which project?")

    def test_none_without_assistant_text(self) -> None:
        self.assertIsNone(
            last_assistant_text([{"role": "user", "content": "hi"}, {"role": "assistant", "content": ""}])
        )
