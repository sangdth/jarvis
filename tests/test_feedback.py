import json
import tempfile
import unittest
from pathlib import Path

from jarvis.feedback import FeedbackLog, overlap
from jarvis.state import UserIntent

A, I, M, U = (UserIntent.ADDRESSED_TO_JARVIS, UserIntent.IGNORE_BACKGROUND, UserIntent.MUTE, UserIntent.UNMUTE)  # noqa: E741


class TestFeedbackLog(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.now = 0.0
        self.log = FeedbackLog(Path(self.dir.name) / "feedback" / "intent.jsonl", clock=lambda: self.now)
        self.addCleanup(self.log.close)

    def heard(self, at: float, text: str, intent: UserIntent, source: str = "laya") -> str:
        self.now = at
        return self.log.decision(text, None, intent, source, laya={"choice": intent.value})

    def corrections(self) -> list[dict]:
        lines = [json.loads(line) for line in self.log.path.read_text().splitlines()]
        return [line for line in lines if "correction_of" in line]

    def test_writes_a_private_line_per_decision(self) -> None:
        self.heard(0, "Push it", A)
        line = json.loads(self.log.path.read_text())
        self.assertEqual(line["state"], {"document": "Push it"})
        self.assertEqual(line["acted"], {"intent": "ADDRESSED_TO_JARVIS", "source": "laya"})
        self.assertEqual(self.log.path.stat().st_mode & 0o777, 0o600)

    def test_unmute_soon_after_a_model_mute_says_it_was_not_a_mute(self) -> None:
        muted = self.heard(0, "Stop it.", M)
        self.heard(12, "you can talk", U, "fast")
        self.assertEqual(
            self.corrections(),
            [{"correction_of": muted, "signal": "unmuted within 30s", "strength": "strong", "not_label": "MUTE"}],
        )

    def test_mute_soon_after_a_model_unmute_says_it_was_not_an_unmute(self) -> None:
        unmuted = self.heard(0, "go on then", U)
        self.heard(5, "shut up", M, "fast")
        self.assertEqual(self.corrections()[0]["correction_of"], unmuted)
        self.assertEqual(self.corrections()[0]["not_label"], "UNMUTE")

    def test_a_fixed_phrase_mute_is_never_corrected(self) -> None:
        self.heard(0, "shut up", M, "fast")
        self.heard(5, "you can talk", U, "fast")
        self.assertEqual(self.corrections(), [])

    def test_a_later_shut_up_vouches_for_the_mute(self) -> None:
        self.heard(0, "pipe down", M)
        self.heard(5, "shut up", M, "fast")
        self.heard(10, "you can talk", U, "fast")
        self.assertEqual(self.corrections(), [])

    def test_unmute_after_the_window_corrects_nothing(self) -> None:
        self.heard(0, "Stop it.", M)
        self.heard(31, "you can talk", U, "fast")
        self.assertEqual(self.corrections(), [])

    def test_not_you_marks_the_last_addressed_sentence_as_room_talk(self) -> None:
        taken = self.heard(0, "Did you see the game?", A)
        self.heard(4, "Not you.", I, "not-you")
        self.assertEqual(
            self.corrections(),
            [{"correction_of": taken, "signal": "not you", "strength": "strong", "label": "IGNORE_BACKGROUND"}],
        )

    def test_saying_an_ignored_sentence_again_marks_it_addressed(self) -> None:
        ignored = self.heard(0, "open the laya project", I)
        self.heard(6, "Jarvis, open the laya project", A)
        self.assertEqual(
            self.corrections(),
            [{"correction_of": ignored, "signal": "said again", "strength": "medium", "label": "ADDRESSED_TO_JARVIS"}],
        )

    def test_a_different_sentence_after_an_ignore_is_not_a_repeat(self) -> None:
        self.heard(0, "honey where are the keys", I)
        self.heard(6, "Jarvis, what's the weather", A)
        self.assertEqual(self.corrections(), [])

    def test_a_decision_is_corrected_once(self) -> None:
        self.heard(0, "Stop it.", M)
        self.heard(5, "you can talk", U, "fast")
        self.heard(8, "you can talk", U, "fast")
        self.assertEqual(len(self.corrections()), 1)

    def test_late_jev_answer_points_at_its_decision(self) -> None:
        decision = self.heard(0, "Push it", A)
        self.log.late(decision, {"choice": "IGNORE_BACKGROUND"})
        last = json.loads(self.log.path.read_text().splitlines()[-1])
        self.assertEqual(last, {"late_for": decision, "jev": {"choice": "IGNORE_BACKGROUND"}})

    def test_a_disk_error_does_not_raise(self) -> None:
        log = FeedbackLog(Path("/dev/null/feedback/intent.jsonl"))
        log.decision("hi", None, A, "laya")
        self.assertIsNone(log._file)

    def test_keeps_one_file_handle_open_across_writes_until_closed(self) -> None:
        self.heard(0, "Push it", A)
        first_handle = self.log._file
        self.assertIsNotNone(first_handle)
        self.heard(1, "Run tests", A)
        self.assertIs(self.log._file, first_handle)
        self.log.close()
        self.assertIsNone(self.log._file)

    def test_overlap_counts_the_old_sentences_words(self) -> None:
        self.assertEqual(overlap("open the project", "jarvis open the project now"), 1.0)
        self.assertEqual(overlap("", "anything"), 0.0)


if __name__ == "__main__":
    unittest.main()
