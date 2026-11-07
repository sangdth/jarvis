import tempfile
import unittest
from pathlib import Path

import numpy as np

from jarvis.enroll import suggest_threshold
from jarvis.speaker import FRAME, SAMPLE_RATE, cosine, save_voiceprint, trim_silence


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
            voiceprint = np.load(save_voiceprint([quiet, loud], Path(tmp) / "v.npy"))
        self.assertAlmostEqual(cosine(quiet, voiceprint), cosine(loud, voiceprint))

    def test_suggested_threshold_sits_under_the_lowest_score(self) -> None:
        self.assertEqual(suggest_threshold([0.52, 0.41, 0.47]), 0.36)


if __name__ == "__main__":
    unittest.main()
