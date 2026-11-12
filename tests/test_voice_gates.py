import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
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
        # The "..." hold runs only with Smart Turn off, which the hold tests below need; the developer's .env may differ.
        from jarvis.config import settings

        smart_turn = patch.object(settings, "smart_turn", False)
        smart_turn.start()
        self.addCleanup(smart_turn.stop)

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

    async def test_with_smart_turn_a_trailing_off_sentence_goes_on_at_once(self) -> None:
        from jarvis.config import settings

        with patch.object(settings, "smart_turn", True):  # it already held the audio across the pause
            await self.gate.process_frame(heard("Open the..."), FrameDirection.DOWNSTREAM)
        self.assertEqual(self.turns(), ["Open the..."])

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

    async def test_meeting_mode_hears_the_users_reply_without_wake_word_but_not_the_rooms(self) -> None:
        gate = JarvisIntentGate(self.state, self.filter, last_said=lambda: "Which day should I book?", meeting=True)
        gate.push_frame = AsyncMock()
        await gate.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        chatter = TranscriptionFrame(text="Did you see the game?", user_id="room", timestamp="0")
        await gate.process_frame(chatter, FrameDirection.DOWNSTREAM)
        self.filter.classify_user_utterance.assert_not_awaited()  # the room keeps talking after Jarvis replies
        reply = TranscriptionFrame(text="Tuesday afternoon", user_id="owner", timestamp="0")
        await gate.process_frame(reply, FrameDirection.DOWNSTREAM)
        self.filter.classify_user_utterance.assert_awaited_once_with(
            transcript="Tuesday afternoon", recent_jarvis_question="Which day should I book?"
        )
        pushed = [c.args[0] for c in gate.push_frame.await_args_list if isinstance(c.args[0], TranscriptionFrame)]
        self.assertEqual(
            [f.text for f in pushed],
            ["[Meeting transcript:\n[Room]: Did you see the game?]\n[User]: Tuesday afternoon"],
        )
        self.assertFalse(self.state.room_turn)

    async def test_meeting_mode_marks_a_room_turn_for_the_agent_tools(self) -> None:
        gate = JarvisIntentGate(self.state, self.filter, meeting=True)
        gate.push_frame = AsyncMock()
        ask = TranscriptionFrame(text="Jarvis, yes, approve it.", user_id="room", timestamp="0")
        await gate.process_frame(ask, FrameDirection.DOWNSTREAM)
        self.assertTrue(self.state.room_turn)

    async def test_cleanup_runs_after_the_check_and_before_the_speaker_labels(self) -> None:
        cleaner = MagicMock(clean=AsyncMock(side_effect=lambda t, **_: t.replace("forty two", "42")))
        down = FrameDirection.DOWNSTREAM
        for gate, frame, expected in (
            (JarvisIntentGate(self.state, self.filter, cleaner=cleaner), heard("make it forty two"), "make it 42"),
            (
                JarvisIntentGate(self.state, self.filter, meeting=True, cleaner=cleaner),
                TranscriptionFrame(text="Jarvis, make it forty two", user_id="room", timestamp="0"),
                "[Room]: Jarvis, make it 42",
            ),
        ):
            gate.push_frame = AsyncMock()
            await gate.process_frame(frame, down)
            pushed = [c.args[0].text for c in gate.push_frame.await_args_list if isinstance(c.args[0], TranscriptionFrame)]
            self.assertEqual(pushed, [expected])
        # The speech check (and its feedback log) gets the words as heard.
        self.assertEqual(self.filter.classify_user_utterance.await_args.kwargs["transcript"], "Jarvis, make it forty two")

    async def test_cleanup_skips_sentences_not_passed_on(self) -> None:
        cleaner = MagicMock(clean=AsyncMock(side_effect=lambda t, **_: t))
        gate = JarvisIntentGate(self.state, self.filter, cleaner=cleaner)
        gate.push_frame = AsyncMock()
        for intent in (UserIntent.MUTE, UserIntent.UNMUTE, UserIntent.IGNORE_BACKGROUND, UserIntent.INTRODUCTION):
            self.filter.classify_user_utterance.return_value = intent
            await gate.process_frame(heard("some words said here"), FrameDirection.DOWNSTREAM)
        cleaner.clean.assert_not_awaited()

    async def test_master_introduction_runs_conversational_enrollment_and_greets_known_speaker_next_time(self) -> None:
        spoken: list[str] = []

        async def on_speak(text: str) -> None:
            spoken.append(text)

        self.state.on_speak = on_speak
        down = FrameDirection.DOWNSTREAM

        # 1. Master introduces a new speaker -> INTRODUCTION intent arms enrollment
        from jarvis.config import settings

        self.enterContext(patch.object(settings, "master_check", True))
        self.filter.classify_user_utterance.return_value = UserIntent.INTRODUCTION
        await self.gate.process_frame(heard("Jarvis, let me introduce my friend."), down)
        self.assertTrue(self.state.enrolling)
        self.assertEqual(spoken, [])

        # 2. New speaker starts speaking -> Jarvis says "Let's have a chat so I can remember your voice" + Q1
        self.filter.classify_user_utterance.return_value = UserIntent.ADDRESSED_TO_JARVIS
        self.gate.registry.observe(np.array([0.0, 1.0], dtype=np.float32), 0.35)
        await self.gate.process_frame(TranscriptionFrame(text="Hello Jarvis!", user_id="voice_1", timestamp="0"), down)
        self.assertEqual(len(spoken), 1)
        self.assertIn("Let's have a chat so I can remember your voice.", spoken[-1])
        self.assertIn("How is your day going so far?", spoken[-1])

        # 3. New speaker answers Q1 -> Jarvis asks Q2 + asks for their name
        await self.gate.process_frame(
            TranscriptionFrame(text="My day is going great, thanks.", user_id="voice_1", timestamp="0"), down
        )
        self.assertEqual(len(spoken), 2)
        self.assertIn("What do you enjoy doing in your free time?", spoken[-1])
        self.assertIn("What should I call you?", spoken[-1])

        # 4. New speaker answers Q2 and states "I'm David" -> identity linked to profile, enrollment done
        await self.gate.process_frame(
            TranscriptionFrame(text="I like hiking, and I'm David.", user_id="voice_1", timestamp="0"), down
        )
        self.assertFalse(self.state.enrolling)
        profile = self.gate.registry.get("David")
        assert profile is not None
        self.assertTrue(profile.enrolled)
        self.assertEqual(profile.name, "David")
        self.assertIn("David", spoken[-1])

        # 5. Next time David speaks -> greeted by name once, and the request itself still reaches the model
        await self.gate.process_frame(
            TranscriptionFrame(text="Jarvis, what's the weather like?", user_id="David", timestamp="0"), down
        )
        self.assertEqual(spoken[-1], "Hi David! Good to hear you again.")
        self.assertEqual(self.turns(), ["[Speaker: David]: Jarvis, what's the weather like?"])
        self.assertTrue(self.state.room_turn)

        # 6. Small talk never renames a known guest; a second turn gets no second greeting
        await self.gate.process_frame(
            TranscriptionFrame(text="It's Friday, I'm Vietnamese by the way.", user_id="David", timestamp="0"), down
        )
        self.assertEqual(profile.name, "David")
        self.assertEqual(len([s for s in spoken if s.startswith("Hi David")]), 1)

    async def test_unknown_voice_after_the_enrollment_window_is_dropped(self) -> None:
        from jarvis import voice_gates
        from jarvis.config import settings

        self.enterContext(patch.object(settings, "master_check", True))

        self.filter.classify_user_utterance.return_value = UserIntent.INTRODUCTION
        await self.gate.process_frame(heard("Jarvis, this is my friend."), FrameDirection.DOWNSTREAM)
        self.filter.classify_user_utterance.return_value = UserIntent.ADDRESSED_TO_JARVIS
        assert self.gate._enrollment is not None
        self.gate._enrollment.started_at -= voice_gates.ENROLL_TIMEOUT_SECS + 1
        self.gate.registry.observe(np.array([0.0, 1.0], dtype=np.float32), 0.35)  # the TV's fingerprint, voice_1
        await self.gate.process_frame(
            TranscriptionFrame(text="Tonight on the news", user_id="voice_1", timestamp="0"), FrameDirection.DOWNSTREAM
        )
        self.assertFalse(self.state.enrolling)
        self.assertEqual(self.turns(), [])
        self.filter.classify_user_utterance.assert_awaited_once()  # only the master's introduction

    async def test_introduction_without_the_master_check_is_a_normal_turn(self) -> None:
        from jarvis.config import settings

        self.filter.classify_user_utterance.return_value = UserIntent.INTRODUCTION
        with patch.object(settings, "master_check", False):
            await self.gate.process_frame(heard("Jarvis, meet Sarah."), FrameDirection.DOWNSTREAM)
        self.assertFalse(self.state.enrolling)
        self.assertEqual(self.turns(), ["Jarvis, meet Sarah."])

    async def test_speaker_stating_name_early_links_identity_and_completes_after_questions(self) -> None:
        spoken: list[str] = []

        async def on_speak(text: str) -> None:
            spoken.append(text)

        self.state.on_speak = on_speak
        down = FrameDirection.DOWNSTREAM

        from jarvis.config import settings

        self.enterContext(patch.object(settings, "master_check", True))
        self.filter.classify_user_utterance.return_value = UserIntent.INTRODUCTION
        await self.gate.process_frame(heard("Jarvis, meet my friend."), down)

        # Speaker states "My name is David" on the very first turn
        self.gate.registry.observe(np.array([0.0, 1.0], dtype=np.float32), 0.35)
        await self.gate.process_frame(
            TranscriptionFrame(text="Hi Jarvis, my name is David.", user_id="voice_1", timestamp="0"), down
        )
        profile = self.gate.registry.get("David")
        assert profile is not None
        self.assertEqual(profile.name, "David")
        self.assertIn("Let's have a chat so I can remember your voice.", spoken[-1])

        # Answers Q1 -> Jarvis asks Q2 without needing to ask their name again
        await self.gate.process_frame(
            TranscriptionFrame(text="Pretty good day so far.", user_id="voice_1", timestamp="0"), down
        )
        self.assertNotIn("What should I call you?", spoken[-1])

        # Answers Q2 -> enrollment finishes and remembers David
        await self.gate.process_frame(
            TranscriptionFrame(text="I enjoy playing tennis.", user_id="voice_1", timestamp="0"), down
        )
        self.assertTrue(profile.enrolled)
        self.assertFalse(self.state.enrolling)
        self.assertIn("Nice to meet you, David!", spoken[-1])


class TestSpeakerVerifiedWhisperSTT(unittest.IsolatedAsyncioTestCase):
    """Whisper is mocked; the embedder returns a fixed vector so the score is known."""

    def setUp(self) -> None:
        from jarvis.config import settings

        # Clip saving stays off unless a test turns it on: the developer's own .env may set SAVE_SEGMENTS.
        saving = patch.object(settings, "save_segments", 0)
        saving.start()
        self.addCleanup(saving.stop)

    async def run_stt(
        self, text: str | None, embedding, *, meeting: bool = False, check: bool = True, registry=None, state=None
    ) -> list:
        import numpy as np
        from pipecat.services.whisper.stt import WhisperSTTServiceMLX

        from jarvis.voice_gates import SpeakerVerifiedWhisperSTT

        async def whisper(_self, _audio):
            if text is not None:  # None: Whisper dropped the segment (no_speech_prob), so no frame at all
                yield heard(text)

        embedder = MagicMock()
        embedder.embed.return_value = None if embedding is None else np.array(embedding, dtype=np.float32)
        stt = SpeakerVerifiedWhisperSTT(
            embedder=embedder if check else None,
            voiceprint=np.array([1.0, 0.0], dtype=np.float32) if check else None,
            threshold=0.5,
            meeting=meeting,
            registry=registry,
            state=state,
        )
        self.embedder = embedder
        self.stt = stt
        with patch.object(WhisperSTTServiceMLX, "run_stt", new=whisper):
            return [f async for f in stt.run_stt(b"\x00\x00" * 16000)]

    async def test_master_check_off_passes_any_voice_unscored(self) -> None:
        frames = await self.run_stt("build a login page", [0.1, 0.9], check=False)
        self.assertEqual([f.text for f in frames], ["build a login page"])
        self.embedder.embed.assert_not_called()

    async def test_master_check_off_in_a_meeting_counts_everyone_as_the_master(self) -> None:
        frames = await self.run_stt("what about the deadline", [0.1, 0.9], meeting=True, check=False)
        self.assertEqual([(f.user_id, f.text) for f in frames], [("owner", "what about the deadline")])

    async def test_owner_voice_passes(self) -> None:
        frames = await self.run_stt("build a login page", [0.9, 0.1])
        self.assertEqual([f.text for f in frames], ["build a login page"])

    async def test_other_voice_is_dropped(self) -> None:
        self.assertEqual(await self.run_stt("build a login page", [0.1, 0.9]), [])
        # Not enrolling: the stranger's voice is not kept, so hours of TV never grow the registry
        self.assertEqual(self.stt._registry.profiles, {})

    async def test_enrolling_passes_unknown_voice_and_known_speaker_passes_later(self) -> None:
        from jarvis.speaker import SpeakerRegistry

        registry = SpeakerRegistry(folder=None)
        state = JarvisState()
        state.enrolling = True

        # During enrollment, the new voice passes tagged with its profile id
        frames = await self.run_stt("Hi, I'm David", [0.0, 1.0], registry=registry, state=state)
        self.assertEqual([(f.user_id, f.text) for f in frames], [("voice_1", "Hi, I'm David")])

        # Once linked and enrolled as "David", it passes even after enrolling is turned off
        registry.link_name("voice_1", "David", enroll=True)
        state.enrolling = False
        frames = await self.run_stt("Hello Jarvis", [0.05, 0.99], registry=registry, state=state)
        self.assertEqual([(f.user_id, f.text) for f in frames], [("David", "Hello Jarvis")])

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


class TestSmartTurn(unittest.IsolatedAsyncioTestCase):
    """The turn analyzer is mocked; Pipecat's flush (one Whisper segment per call) is counted, not run."""

    def setUp(self) -> None:
        from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
        from pipecat.services.stt_service import SegmentedSTTService

        from jarvis.voice_gates import SpeakerVerifiedWhisperSTT

        self.states = EndOfTurnState
        self.turn = MagicMock()
        self.turn.analyze_end_of_turn = AsyncMock(return_value=(EndOfTurnState.COMPLETE, None))
        self.turn.append_audio.return_value = EndOfTurnState.INCOMPLETE
        self.stt = SpeakerVerifiedWhisperSTT(embedder=None, voiceprint=None, threshold=0.5, turn_analyzer=self.turn)
        self.stt._audio_buffer_size_1s = 4  # normally one second; tiny so a trim would show
        self.stt._sample_rate = 16000  # set by StartFrame in the pipeline
        flush = patch.object(SegmentedSTTService, "_handle_user_stopped_speaking", new_callable=AsyncMock)
        self.flush = flush.start()
        self.addCleanup(flush.stop)

    async def speak(self, audio: bytes) -> None:
        from pipecat.frames.frames import InputAudioRawFrame

        await self.stt._handle_user_started_speaking(VADUserStartedSpeakingFrame())
        await self.stt.process_audio_frame(InputAudioRawFrame(audio, 16000, 1), FrameDirection.DOWNSTREAM)
        await self.stt._handle_user_stopped_speaking(VADUserStoppedSpeakingFrame())

    async def test_a_finished_sentence_goes_on_at_the_short_pause(self) -> None:
        await self.speak(b"\x01\x00" * 8)
        self.flush.assert_awaited_once()

    async def test_a_pause_mid_thought_keeps_the_whole_sentence_for_one_segment(self) -> None:
        self.turn.analyze_end_of_turn.return_value = (self.states.INCOMPLETE, None)
        await self.speak(b"\x01\x00" * 8)  # "open the..."
        self.flush.assert_not_awaited()
        self.turn.analyze_end_of_turn.return_value = (self.states.COMPLETE, None)
        await self.speak(b"\x02\x00" * 8)  # "...jarvis project"
        self.flush.assert_awaited_once()
        self.assertEqual(bytes(self.stt._audio_buffer), b"\x01\x00" * 8 + b"\x02\x00" * 8)  # no one-second trim

    async def test_long_silence_after_a_pause_sends_what_was_said(self) -> None:
        from pipecat.frames.frames import InputAudioRawFrame

        self.turn.analyze_end_of_turn.return_value = (self.states.INCOMPLETE, None)
        await self.speak(b"\x01\x00" * 8)
        self.turn.append_audio.return_value = self.states.COMPLETE  # past SmartTurnParams.stop_secs of silence
        await self.stt.process_audio_frame(InputAudioRawFrame(b"\x00\x00" * 8, 16000, 1), FrameDirection.DOWNSTREAM)
        self.flush.assert_awaited_once()
        self.turn.analyze_end_of_turn.assert_awaited_once()  # not asked again: it has no audio left to judge

    async def test_a_sentence_held_past_the_cap_goes_on_anyway(self) -> None:
        from jarvis.voice_gates import MAX_HELD_SECS

        self.turn.analyze_end_of_turn.return_value = (self.states.INCOMPLETE, None)
        self.stt._audio_buffer = bytearray(16000 * 2 * MAX_HELD_SECS)  # a TV that never sounds done
        await self.speak(b"\x01\x00" * 8)
        self.flush.assert_awaited_once()

    async def test_without_smart_turn_every_pause_ends_the_sentence(self) -> None:
        from jarvis.voice_gates import SpeakerVerifiedWhisperSTT

        self.stt = SpeakerVerifiedWhisperSTT(embedder=None, voiceprint=None, threshold=0.5)
        await self.speak(b"\x01\x00" * 8)
        self.flush.assert_awaited_once()


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
