import hashlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from jarvis_voice.speaker import (
    EPHEMERAL_TTL_SECS,
    FRAME,
    MAX_EPHEMERAL,
    SAMPLE_RATE,
    SpeakerBeliefTracker,
    SpeakerRegistry,
    VoiceProfile,
    cosine,
    ensure_model,
    estimate_pitch,
    extract_claimed_name,
    extract_correction,
    extract_intro_cue,
    extract_introduced_name,
    extract_speaker_name,
    speaker_number,
    trim_silence,
)


class TestSpeaker(unittest.TestCase):
    def test_trim_silence_keeps_the_speech_and_a_short_pad(self) -> None:
        rng = np.random.default_rng(0)
        hiss = rng.normal(0, 0.001, SAMPLE_RATE)  # 1 s of quiet room noise
        speech = rng.normal(0, 0.2, SAMPLE_RATE)
        trimmed = trim_silence(np.concatenate([hiss, speech, hiss]).astype(np.float32))
        self.assertEqual(len(trimmed), SAMPLE_RATE + 2 * 5 * FRAME)  # the speech plus 100 ms each side

    def test_trim_silence_leaves_silence_and_tiny_clips_alone(self) -> None:
        self.assertEqual(len(trim_silence(np.zeros(SAMPLE_RATE, dtype=np.float32))), SAMPLE_RATE)
        self.assertEqual(len(trim_silence(np.ones(10, dtype=np.float32))), 10)

    def test_extract_speaker_name_recognizes_self_introductions_and_ignores_adjectives(self) -> None:
        self.assertEqual(extract_speaker_name("Hi Jarvis, I'm David."), "David")
        self.assertEqual(extract_speaker_name("Hello, my name is David!"), "David")
        self.assertEqual(extract_speaker_name("You can call me Sarah."), "Sarah")
        self.assertEqual(extract_speaker_name("Mình là Minh."), "Minh")
        self.assertEqual(extract_speaker_name("Tên em là Linh."), "Linh")
        self.assertIsNone(extract_speaker_name("I'm tired today"))
        self.assertIsNone(extract_speaker_name("I'm going to grab a coffee"))
        self.assertIsNone(extract_speaker_name("Build a login page"))
        self.assertEqual(extract_speaker_name("David.", allow_bare=True), "David")
        self.assertEqual(extract_speaker_name("It's David", allow_bare=True), "David")
        self.assertIsNone(extract_speaker_name("I don't know", allow_bare=True))
        self.assertIsNone(extract_speaker_name("Pretty good thanks", allow_bare=True))
        self.assertIsNone(extract_speaker_name("Pretty good thanks"))

    def test_extract_introduced_name_from_master(self) -> None:
        self.assertEqual(extract_introduced_name("Jarvis, this is David."), "David")
        self.assertEqual(extract_introduced_name("Jarvis, this is my friend Sarah."), "Sarah")
        self.assertEqual(extract_introduced_name("Hey Jarvis, meet Minh."), "Minh")
        self.assertIsNone(extract_introduced_name("Jarvis, let me introduce my friend."))
        self.assertIsNone(extract_introduced_name("Jarvis, I want to introduce someone to you."))

    def test_intro_cue_reads_the_name_and_the_pronoun(self) -> None:
        self.assertEqual(extract_intro_cue("This is John, he is my friend."), ("John", "male"))
        self.assertEqual(extract_intro_cue("Jarvis, this is my sister Sarah."), ("Sarah", "female"))
        self.assertEqual(extract_intro_cue("Jarvis, meet Minh."), ("Minh", None))

    def test_claimed_name_avoids_weekdays_and_other_idioms(self) -> None:
        self.assertEqual(extract_claimed_name("Hi, I'm John, nice to meet you.", []), "John")
        self.assertEqual(extract_claimed_name("My name is David.", []), "David")
        self.assertEqual(extract_claimed_name("I'm David.", [], allow_bare=True), "David")
        self.assertIsNone(extract_claimed_name("I'm David.", []))
        self.assertIsNone(extract_claimed_name("It's Friday, I'm Vietnamese by the way.", []))

    def test_correction_reads_the_new_and_the_wrong_name(self) -> None:
        self.assertEqual(extract_correction("No, actually, I'm Mike."), ("Mike", None))
        self.assertEqual(extract_correction("I'm not John."), (None, "John"))

    def test_pitch_separates_a_low_and_a_high_voice(self) -> None:
        rate = SAMPLE_RATE
        t = np.arange(rate) / rate
        low = (0.3 * np.sin(2 * np.pi * 120 * t)).astype(np.float32)
        high = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        self.assertAlmostEqual(estimate_pitch(low), 120.0, delta=4.0)
        self.assertAlmostEqual(estimate_pitch(high), 220.0, delta=4.0)
        self.assertIsNone(estimate_pitch(np.zeros(rate, dtype=np.float32)))

    def test_a_natural_introduction_scores_the_new_voice(self) -> None:
        registry = SpeakerRegistry(folder=None)

        def belief(transcript: str, pitch: float | None, speech_secs: float):
            tracker = SpeakerBeliefTracker()
            tracker.note_intro("John", "male", now=1000.0)
            profile = VoiceProfile(
                id="voice_1", voiceprint=np.array([0.0, 1.0], dtype=np.float32), speech_secs=speech_secs
            )
            registry.profiles = {profile.id: profile}
            return tracker.observe_guest(
                profile, transcript, pitch_hz=pitch, now=1005.0, meeting=False, registry=registry
            )

        # "This is John, he is my friend" then "Hi, nice to meet you": a good guess, not certainty yet.
        guess = belief("Hi, nice to meet you, Jarvis.", 120.0, 2.0)
        self.assertEqual(guess.name, "John")
        self.assertGreater(guess.confidence, 0.65)
        self.assertLess(guess.confidence, 0.75)

        # "Hi, I'm John" confirms it.
        stated = belief("Hi, I'm John, nice to meet you.", 120.0, 2.0)
        self.assertEqual(stated.name, "John")
        self.assertGreater(stated.confidence, 0.98)

    def test_a_pitch_mismatch_beats_the_introduction_hint(self) -> None:
        registry = SpeakerRegistry(folder=None)
        tracker = SpeakerBeliefTracker()
        tracker.note_intro("John", "male", now=1000.0)
        profile = VoiceProfile(id="voice_1", voiceprint=np.array([0.0, 1.0], dtype=np.float32), speech_secs=2.0)
        registry.profiles = {profile.id: profile}
        belief = tracker.observe_guest(
            profile, "Hi, nice to meet you.", pitch_hz=230.0, now=1005.0, meeting=False, registry=registry
        )
        self.assertLess(belief.confidence, 0.50)

    def test_a_correction_moves_the_name_to_the_right_voice(self) -> None:
        registry = SpeakerRegistry(folder=None)
        tracker = SpeakerBeliefTracker()
        tracker.note_intro("John", "male", now=1000.0)
        profile = VoiceProfile(id="voice_1", voiceprint=np.array([0.0, 1.0], dtype=np.float32), speech_secs=2.0)
        registry.profiles = {profile.id: profile}
        tracker.observe_guest(profile, "Hi, I'm Mike.", pitch_hz=120.0, now=1005.0, meeting=False, registry=registry)
        belief = tracker.observe_guest(
            profile, "No, actually, I'm John.", pitch_hz=120.0, now=1006.0, meeting=False, registry=registry
        )
        self.assertEqual(belief.name, "John")
        self.assertGreater(belief.confidence, 0.95)

    def test_unnamed_voices_expire_and_stay_bounded(self) -> None:
        registry = SpeakerRegistry(folder=None)
        first = registry.observe(np.eye(12)[0].astype(np.float32), 0.5, now=1000.0)
        assert first is not None
        registry.observe(np.eye(12)[1].astype(np.float32), 0.5, now=1000.0 + EPHEMERAL_TTL_SECS + 1)
        self.assertNotIn(first.id, registry.profiles, "an idle unnamed voice is dropped")

        for index in range(MAX_EPHEMERAL + 3):
            registry.observe(np.eye(12)[index].astype(np.float32), 0.5, now=2000.0 + index)
        anonymous = [p for p in registry.profiles.values() if not p.enrolled]
        self.assertEqual(len(anonymous), MAX_EPHEMERAL)

    def test_a_confident_name_is_persisted_only_with_enough_speech(self) -> None:
        def graduate(speech_secs: float) -> bool:
            registry = SpeakerRegistry(folder=None)
            tracker = SpeakerBeliefTracker()
            tracker.note_intro("John", "male", now=1000.0)
            profile = VoiceProfile(
                id="voice_1", voiceprint=np.array([0.0, 1.0], dtype=np.float32), speech_secs=speech_secs
            )
            registry.profiles = {profile.id: profile}
            belief = tracker.observe_guest(
                profile, "Hi, I'm John.", pitch_hz=120.0, now=1005.0, meeting=False, registry=registry
            )
            return belief.graduated

        self.assertFalse(graduate(1.0), "99% on a short 'Hi' names this session but does not write a voiceprint")
        self.assertTrue(graduate(5.0))
        self.assertEqual(speaker_number(VoiceProfile(id="voice_7", voiceprint=np.zeros(2))), 7)

    def test_speaker_registry_creates_profiles_links_identity_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "speakers"
            reg = SpeakerRegistry(folder=folder)

            # First utterance from an unknown voice creates a fingerprint profile
            p1 = reg.observe(np.array([0.0, 1.0], dtype=np.float32), threshold=0.5)
            assert p1 is not None
            self.assertEqual(p1.id, "voice_1")
            self.assertIsNone(p1.name)
            self.assertEqual(p1.clips, 1)

            # Second utterance from the same voice matches and refines the profile
            p2 = reg.observe(np.array([0.1, 0.99], dtype=np.float32), threshold=0.5)
            self.assertIs(p2, p1)
            self.assertEqual(p1.clips, 2)

            # Linking a name ("David") saves the profile to disk
            linked = reg.link_name(p1.id, "David", enroll=True)
            assert linked is not None
            self.assertEqual(linked.name, "David")
            self.assertTrue(linked.enrolled)

            # A fresh SpeakerRegistry loads the saved profile and matches David's voice
            reloaded = SpeakerRegistry(folder=folder)
            matched = reloaded.match(np.array([0.05, 0.98], dtype=np.float32), threshold=0.5)
            assert matched is not None
            self.assertEqual(matched.name, "David")
            self.assertTrue(matched.enrolled)

    def test_link_name_merges_newly_observed_profile_into_existing_named_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "speakers"
            reg = SpeakerRegistry(folder=folder)

            p1 = reg.observe(np.array([0.0, 1.0], dtype=np.float32), threshold=0.5)
            assert p1 is not None
            reg.link_name(p1.id, "David", enroll=True)

            # A distinct second embedding creates a new temporary profile, then merges into David's profile on link_name.
            p2 = reg.observe(np.array([1.0, 0.0], dtype=np.float32), threshold=0.5)
            assert p2 is not None
            self.assertNotEqual(p2.id, p1.id)

            merged = reg.link_name(p2.id, "David", enroll=True)
            self.assertIs(merged, p1)
            self.assertEqual(p1.clips, 2)
            self.assertIsNone(reg.get(p2.id))
            self.assertEqual(stat.S_IMODE(os.stat(folder / "speakers.json").st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(folder / f"{p1.id}.npy").st_mode), 0o600)

            reloaded = SpeakerRegistry(folder=folder)
            david = reloaded.get("David")
            assert david is not None
            self.assertEqual(david.clips, 2)
            self.assertGreater(cosine(np.array([0.7, 0.7], dtype=np.float32), david.voiceprint), 0.99)

    def test_link_name_without_enroll_persists_when_merging_into_enrolled_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "speakers"
            reg = SpeakerRegistry(folder=folder)

            p1 = reg.observe(np.array([0.0, 1.0], dtype=np.float32), threshold=0.5)
            assert p1 is not None
            reg.link_name(p1.id, "David", enroll=True)

            p2 = reg.observe(np.array([1.0, 0.0], dtype=np.float32), threshold=0.5)
            assert p2 is not None
            merged = reg.link_name(p2.id, "David", enroll=False)
            self.assertIs(merged, p1)

            reloaded = SpeakerRegistry(folder=folder)
            david = reloaded.get("David")
            assert david is not None
            self.assertEqual(david.clips, 2)
            self.assertGreater(cosine(np.array([0.7, 0.7], dtype=np.float32), david.voiceprint), 0.99)

    def test_ensure_model_downloads_verifies_checksum_and_reuses_valid_cache(self) -> None:
        payload = b"fake-onnx-model"
        digest = hashlib.sha256(payload).hexdigest()
        legacy_payload = b"legacy-onnx-model"
        legacy_digest = hashlib.sha256(legacy_payload).hexdigest()

        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "models" / "model.onnx"
            with (
                patch("jarvis_voice.speaker.MODEL_SHA256", digest),
                patch("jarvis_voice.speaker._VALID_MODEL_SHA256S", {digest, legacy_digest}),
                patch("urllib.request.urlopen", return_value=io.BytesIO(payload)) as urlopen,
            ):
                self.assertEqual(ensure_model(model_path), model_path)
                self.assertEqual(model_path.read_bytes(), payload)
                self.assertEqual(urlopen.call_count, 1)

                # Cached copy with current SHA-256 skips the network.
                self.assertEqual(ensure_model(model_path), model_path)
                self.assertEqual(urlopen.call_count, 1)

                # Cached copy from upstream's earlier export also skips the network.
                model_path.write_bytes(legacy_payload)
                self.assertEqual(ensure_model(model_path), model_path)
                self.assertEqual(urlopen.call_count, 1)

            bad_path = Path(tmp) / "models" / "bad.onnx"
            with (
                patch("jarvis_voice.speaker.MODEL_SHA256", digest),
                patch("jarvis_voice.speaker._VALID_MODEL_SHA256S", {digest}),
                patch("urllib.request.urlopen", return_value=io.BytesIO(b"corrupted")),
            ):
                with self.assertRaises(RuntimeError):
                    ensure_model(bad_path)
                self.assertFalse(bad_path.exists())
                self.assertFalse(bad_path.with_suffix(".part").exists())

    def test_owner_enrollment_requires_longer_chat_and_two_confirmations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vp = Path(tmp) / "owner_voice.npy"
            enrolled: list[VoiceProfile] = []
            reg = SpeakerRegistry(
                folder=None,
                voiceprint_path=vp,
                on_owner_enrolled=enrolled.append,
            )
            self.assertTrue(reg.learning_owner)
            self.assertEqual(reg.owner_enrollment_status()[0], "waiting")

            # One long turn is not enough on its own (needs 4 turns and >= 10s).
            self.assertIsNone(reg.record_owner_clip(np.array([1.0, 0.0], dtype=np.float32), 15.0, "Sang"))
            self.assertEqual(reg.owner_enrollment_status()[:3], ("learning", 1, 4))
            self.assertFalse(vp.exists())

            # Weak clip neither advances turns nor pollutes the voiceprint.
            self.assertIsNone(
                reg.record_owner_clip(np.array([0.0, 1.0], dtype=np.float32), 3.0, score=0.30, threshold=0.35)
            )
            self.assertTrue(reg.owner_enrollment_status()[4])
            self.assertEqual(reg.owner_enrollment_status()[:3], ("learning", 1, 4))
            for _ in range(2):
                self.assertIsNone(
                    reg.record_owner_clip(np.array([0.99, 0.05], dtype=np.float32), 2.5, score=0.95, threshold=0.35)
                )
            self.assertEqual(reg.owner_enrollment_status()[:3], ("learning", 3, 4))

            # Turn 4 reaches >= 4 turns and >= 10s -> enters confirmation phase without saving yet.
            self.assertIsNone(
                reg.record_owner_clip(np.array([0.98, 0.05], dtype=np.float32), 2.5, score=0.95, threshold=0.35)
            )
            self.assertEqual(reg.owner_enrollment_status()[:3], ("confirming", 0, 2))
            self.assertFalse(vp.exists())

            # Weak short answer during confirmation triggers retry without incrementing confirmed count.
            self.assertIsNone(
                reg.record_owner_clip(np.array([0.0, 1.0], dtype=np.float32), 1.0, score=0.28, threshold=0.35)
            )
            self.assertEqual(reg.owner_enrollment_status()[:3], ("confirming", 0, 2))
            self.assertTrue(reg.owner_enrollment_status()[4])
            self.assertFalse(vp.exists())

            # First passing confirmation check advances to 1/2.
            self.assertIsNone(
                reg.record_owner_clip(np.array([0.97, 0.08], dtype=np.float32), 1.5, score=0.80, threshold=0.35)
            )
            self.assertEqual(reg.owner_enrollment_status()[:3], ("confirming", 1, 2))
            self.assertFalse(reg.owner_enrollment_status()[4])
            self.assertFalse(vp.exists())

            # Second passing confirmation check saves owner_voice.npy (0600) and fires callback once.
            profile = reg.record_owner_clip(np.array([0.96, 0.10], dtype=np.float32), 1.5, score=0.78, threshold=0.35)
            assert profile is not None
            self.assertEqual(len(enrolled), 1)
            self.assertEqual(profile.clips, 6)
            self.assertTrue(vp.is_file())
            self.assertEqual(stat.S_IMODE(os.stat(vp).st_mode), 0o600)
            self.assertFalse(reg.learning_owner)
            self.assertEqual(reg.owner_enrollment_status()[0], "just_enrolled")
            self.assertTrue(reg.consume_just_enrolled())
            self.assertFalse(reg.consume_just_enrolled())
            self.assertEqual(reg.owner_enrollment_status()[0], "idle")

    def test_owner_enrollment_needs_both_min_turns_and_min_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vp = Path(tmp) / "owner_voice.npy"
            reg = SpeakerRegistry(folder=None, voiceprint_path=vp, confirm_checks=0)
            # 4 very short turns (1.5s each = 6.0s < 10.0s) stay in learning.
            for _ in range(4):
                self.assertIsNone(
                    reg.record_owner_clip(np.array([1.0, 0.0], dtype=np.float32), 1.5, score=0.95, threshold=0.35)
                )
            self.assertEqual(reg.owner_enrollment_status()[0], "learning")
            self.assertFalse(vp.exists())
            # Reaching >= 10s with confirm_checks=0 finalizes immediately (offline fallback).
            profile = reg.record_owner_clip(np.array([1.0, 0.0], dtype=np.float32), 4.5, score=0.95, threshold=0.35)
            assert profile is not None
            self.assertTrue(vp.is_file())

    def test_enrollment_flow_goodbye_waits_past_barge_in_and_startup_welcomes_without_cancelling(self) -> None:
        from jarvis_voice.main import _EnrollmentFlow

        with tempfile.TemporaryDirectory() as tmp:
            # 1. `jarvis enroll` (standalone=True): barge-in on question 2 does not cancel before goodbye plays.
            reg = SpeakerRegistry(folder=None, voiceprint_path=Path(tmp) / "a.npy")
            flow = _EnrollmentFlow(registry=reg, standalone=True)
            reg.on_owner_enrolled = lambda _p: flow.on_enrolled()

            self.assertEqual(flow.llm_turn(), (None, True))  # waiting on greeting: tools hidden
            for _ in range(4):
                reg.record_owner_clip(np.array([1.0, 0.0], dtype=np.float32), 3.0, score=0.95, threshold=0.35)
            note, hide = flow.llm_turn()
            self.assertTrue(hide)
            self.assertIn("couple of quick questions", note or "")

            reg.record_owner_clip(np.array([1.0, 0.0], dtype=np.float32), 1.5, score=0.95, threshold=0.35)
            reg.record_owner_clip(np.array([1.0, 0.0], dtype=np.float32), 1.5, score=0.95, threshold=0.35)

            # Barge-in on confirmation question 2 arrives before goodbye starts -> must not cancel.
            self.assertFalse(flow.should_cancel_after_spoken())
            # Even if an earlier start frame arrived while farewell was not yet dispatched, do not cancel.
            flow.on_assistant_started()
            self.assertFalse(flow.should_cancel_after_spoken())
            # Real goodbye turn starts and dispatches -> speaks goodbye -> now cancels cleanly.
            flow.on_assistant_started()
            goodbye_note, hide = flow.llm_turn()
            self.assertTrue(hide)
            self.assertIn("goodbye", goodbye_note or "")
            self.assertTrue(flow.should_cancel_after_spoken())
            self.assertFalse(flow.should_cancel_after_spoken())  # one-shot

            # 2. `jarvis start` (standalone=False): welcomes warmly, never cancels, unlocks tools next turn.
            reg2 = SpeakerRegistry(folder=None, voiceprint_path=Path(tmp) / "b.npy")
            flow2 = _EnrollmentFlow(registry=reg2, standalone=False)
            reg2.on_owner_enrolled = lambda _p: flow2.on_enrolled()
            for _ in range(4):
                reg2.record_owner_clip(np.array([1.0, 0.0], dtype=np.float32), 3.0, score=0.95, threshold=0.35)
            for _ in range(2):
                reg2.record_owner_clip(np.array([1.0, 0.0], dtype=np.float32), 1.5, score=0.95, threshold=0.35)
            welcome_note, hide = flow2.llm_turn()
            self.assertTrue(hide)
            self.assertIn("welcome", welcome_note or "")
            self.assertIn("Do not say goodbye", welcome_note or "")
            flow2.on_assistant_started()
            self.assertFalse(flow2.should_cancel_after_spoken())
            self.assertEqual(flow2.llm_turn(), (None, False))  # tools unlocked on the next turn

    def test_owner_correction_regex_catches_self_name_fixes_without_misfiring_on_small_talk(self) -> None:
        from jarvis_voice.speaker import extract_owner_correction

        self.assertEqual(extract_owner_correction("Actually, my name is Sang.", "Song"), "Sang")
        self.assertEqual(extract_owner_correction("No, I'm not Song, it's Sang.", "Song"), "Sang")
        self.assertEqual(extract_owner_correction("My name is not Song, it's Sang.", "Song"), "Sang")
        self.assertIsNone(extract_owner_correction("My name is not Song.", "Song"))
        self.assertIsNone(extract_owner_correction("Actually it's tea.", "Song"))
        self.assertIsNone(extract_speaker_name("My name is not Song."))

    def test_registry_renames_owner_and_guests_and_reloads_disk_edits(self) -> None:
        import json

        from jarvis_core.soul import load_soul, owner_name, remember_owner_name

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "speakers"
            soul_path = Path(tmp) / "SOUL.md"
            remember_owner_name("Song", soul_path)
            reg = SpeakerRegistry(folder=folder, owner_name="Song", soul_path=soul_path)

            res = reg.set_owner_name("Sang Do")
            assert res is not None
            self.assertEqual((res.profile_id, res.previous, res.name), ("owner", "Song", "Sang Do"))
            self.assertEqual(owner_name(load_soul(soul_path).personality), "Sang Do")
            self.assertIn("- Owner: Sang Do", reg.format_for_prompt())

            # External edit to SOUL.md is picked up live without restarting.
            remember_owner_name("Sang", soul_path, overwrite=True)
            self.assertIn("- Owner: Sang", reg.format_for_prompt())
            self.assertEqual(reg.owner_name, "Sang")

            # Rename an ephemeral voice by its dashboard label ("Speaker 1") -> enrolled & saved on disk.
            p1 = reg.observe(np.array([0.0, 1.0], dtype=np.float32), threshold=0.5)
            assert p1 is not None
            renamed = reg.rename_profile("Speaker 1", "Davd", enroll=True)
            assert renamed is not None
            self.assertEqual(
                (renamed.profile_id, renamed.previous, renamed.name, renamed.enrolled),
                ("voice_1", "Speaker 1", "Davd", True),
            )

            # Non-owner self-rename cannot merge into a different enrolled profile.
            p2 = reg.observe(np.array([1.0, 0.0], dtype=np.float32), threshold=0.5)
            assert p2 is not None
            self.assertIsNone(reg.rename_profile(p2.id, "Davd", enroll=False, allow_merge=False))

            # External edit to speakers.json is picked up live without restarting.
            idx_path = folder / "speakers.json"
            data = json.loads(idx_path.read_text(encoding="utf-8"))
            data["voice_1"]["name"] = "David"
            idx_path.write_text(json.dumps(data), encoding="utf-8")
            self.assertIn("- Guests: David", reg.format_for_prompt())
            self.assertEqual(reg.get("voice_1").name, "David")  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
