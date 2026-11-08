import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "voice"))
from record import read_sentences, split_of  # noqa: E402


class TestVoiceRecord(unittest.TestCase):
    def test_comments_blanks_and_repeats_are_skipped_in_order(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# heading\nMerge the PR.\n\n  Rebase on master.  \nMerge the PR.\n")
        self.assertEqual(read_sentences(Path(f.name)), ["Merge the PR.", "Rebase on master."])

    def test_split_is_fixed_by_the_text_and_holds_out_about_one_in_seven(self) -> None:
        texts = [f"sentence {i}" for i in range(700)]
        self.assertEqual([split_of(t) for t in texts], [split_of(t) for t in texts])
        held_out = sum(split_of(t) == "test" for t in texts)
        self.assertTrue(70 <= held_out <= 130, held_out)


if __name__ == "__main__":
    unittest.main()
