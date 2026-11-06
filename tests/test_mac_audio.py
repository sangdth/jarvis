import unittest

import numpy as np

from jarvis.mac_audio import MicConverter


class TestMicConverter(unittest.TestCase):
    """The processed mic (float32 at 48 kHz) reaches Jarvis as 16-bit PCM at 16 kHz."""

    def test_48k_float_becomes_16k_pcm(self) -> None:
        mic = MicConverter(16000)
        tone = np.sin(np.arange(4800) * 0.05).astype(np.float32) * 0.5  # 100 ms at 48 kHz
        out = b"".join(mic.convert(tone[i : i + 960], 48000) for i in range(0, 4800, 960))  # 20 ms blocks
        self.assertAlmostEqual(len(out) / 2, 1600, delta=200)  # 100 ms at 16 kHz; the stream holds back a little
        self.assertLess(np.abs(np.frombuffer(out, dtype=np.int16)).max(), 32767 * 0.6)  # same level, not clipped

    def test_same_rate_only_changes_the_sample_format(self) -> None:
        out = MicConverter(16000).convert(np.array([0.0, 0.5, -1.0, 2.0], dtype=np.float32), 16000)
        self.assertEqual(np.frombuffer(out, dtype=np.int16).tolist(), [0, 16383, -32767, 32767])  # 2.0 is clipped


class TestMicHeard(unittest.TestCase):
    """A dead voice-processing mic gives nothing or near zeros; room noise counts as a working mic."""

    def test_near_zeros_are_not_heard(self) -> None:
        mic = MicConverter(16000)
        for _ in range(150):  # 3 s of 20 ms blocks at 48 kHz
            mic.convert(np.full(960, 3e-5, dtype=np.float32), 48000)
        self.assertFalse(mic.heard)

    def test_one_block_of_room_noise_is_heard_for_good(self) -> None:
        mic = MicConverter(16000)
        mic.convert(np.full(960, 5e-3, dtype=np.float32), 48000)
        mic.convert(np.zeros(960, dtype=np.float32), 48000)
        self.assertTrue(mic.heard)


if __name__ == "__main__":
    unittest.main()
