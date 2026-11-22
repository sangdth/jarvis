import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from jarvis_voice import agent_tools, meeting_notes


class MeetingNotesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.day = date(2026, 11, 22)
        self.now = datetime(2026, 11, 22, 16, 42)

    def test_a_comment_is_written_with_who_said_it_and_when(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path, count = meeting_notes.append_comment(
                "we are going with Postgres", "Speaker: David", day=self.day, directory=directory, now=self.now
            )
            self.assertEqual(path, directory / "2026-11-22.md")
            self.assertEqual(count, 1)
            text = path.read_text()
            self.assertIn("# Meeting notes — Sunday 22 November 2026", text)
            self.assertIn("- **16:42 · David:** we are going with Postgres", text)
            self.assertEqual(directory.stat().st_mode & 0o700, 0o700)  # the notes folder stays private

    def test_the_header_is_written_once_and_the_count_grows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            meeting_notes.append_comment("first", "User", day=self.day, directory=directory, now=self.now)
            path, count = meeting_notes.append_comment("second", "Room", day=self.day, directory=directory, now=self.now)
            text = path.read_text()
            self.assertEqual(count, 2)
            self.assertEqual(text.count("# Meeting notes"), 1)
            self.assertIn("You:** first", text)
            self.assertIn("The room:** second", text)

    def test_whitespace_is_collapsed_and_empty_comments_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path, _ = meeting_notes.append_comment("  two\n  lines  ", "David", day=self.day, directory=directory)
            self.assertIn("David:** two lines", path.read_text())
            with self.assertRaises(ValueError):
                meeting_notes.append_comment("   ", "David", day=self.day, directory=directory)

    def test_reading_back_returns_the_day_or_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertEqual(meeting_notes.read_comments(self.day, directory=directory), "")
            meeting_notes.append_comment("ship it Friday", "David", day=self.day, directory=directory)
            self.assertIn("ship it Friday", meeting_notes.read_comments(self.day, directory=directory))
            self.assertEqual(meeting_notes.read_comments(date(2026, 11, 21), directory=directory), "")

    def test_speaker_labels_read_like_a_name(self) -> None:
        self.assertEqual(meeting_notes.clean_speaker("Speaker: David"), "David")
        self.assertEqual(meeting_notes.clean_speaker("Room"), "The room")
        self.assertEqual(meeting_notes.clean_speaker("User"), "You")
        self.assertEqual(meeting_notes.clean_speaker(""), "Someone")


class CommentToolsTest(unittest.TestCase):
    def _names(self, tools) -> list[str]:
        return [getattr(tool, "name", "") for tool in tools]

    def test_the_comment_tools_exist_only_in_a_meeting(self) -> None:
        self.assertNotIn("add_comment", self._names(agent_tools.agent_tools()))
        self.assertNotIn("read_comments", self._names(agent_tools.agent_tools()))
        meeting = self._names(agent_tools.agent_tools(meeting=True))
        self.assertIn("add_comment", meeting)
        self.assertIn("read_comments", meeting)
        self.assertIn("send_agent_task", meeting)  # the meeting tools are additive, not a mode switch

    def test_the_comment_schema_asks_for_the_note_only(self) -> None:
        tool = next(t for t in agent_tools.comment_tools() if t.name == "add_comment")
        self.assertEqual(tool.required, ["comment"])
        self.assertNotIn("agent_id", tool.properties)


if __name__ == "__main__":
    unittest.main()
