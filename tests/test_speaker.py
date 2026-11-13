import os
import stat
import tempfile
import unittest
from pathlib import Path

import numpy as np

from jarvis.enroll import suggest_threshold
from jarvis.speaker import (
    FRAME,
    SAMPLE_RATE,
    SpeakerRegistry,
    cosine,
    extract_introduced_name,
    extract_speaker_name,
    save_voiceprint,
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

    def test_a_loud_clip_does_not_outweigh_the_others(self) -> None:
        quiet, loud = np.array([1.0, 0.0]), np.array([0.0, 100.0])
        with tempfile.TemporaryDirectory() as tmp:
            path = save_voiceprint([quiet, loud], Path(tmp) / "sub" / "v.npy")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
            voiceprint = np.load(path)
        self.assertAlmostEqual(cosine(quiet, voiceprint), cosine(loud, voiceprint))

    def test_suggested_threshold_sits_under_the_lowest_score(self) -> None:
        self.assertEqual(suggest_threshold([0.52, 0.41, 0.47]), 0.36)

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


if __name__ == "__main__":
    unittest.main()
