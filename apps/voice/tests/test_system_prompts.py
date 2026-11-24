import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis_voice.system_prompts import build_retell_prompt, load_overrides, parse_overrides


class TestSystemPrompts(unittest.TestCase):
    def test_no_file_means_no_overrides(self) -> None:
        self.assertEqual(load_overrides(Path(tempfile.gettempdir()) / "no-such-SYSTEM.md"), {})

    def test_a_cleanup_section_overrides_and_its_name_ignores_case(self) -> None:
        text = "# My prompts\nintro text\n\n## CleanUp\nFix punctuation only.\nKeep fillers.\n"
        self.assertEqual(parse_overrides(text), {"cleanup": "Fix punctuation only.\nKeep fillers."})

    def test_empty_and_unknown_sections_are_left_out(self) -> None:
        self.assertEqual(parse_overrides("## Cleanup\n\n## Cleanp\nTypo.\n"), {})

    def test_non_empty_file_with_no_matching_section_warns(self) -> None:
        for text in ("# Cleanup\nOnly commas.\n", "Only commas.\n", "## Cleanup\n\n"):
            with self.subTest(text), patch("jarvis_voice.system_prompts.logger") as logger:
                self.assertEqual(parse_overrides(text), {})
                logger.warning.assert_called_once()

    def test_reads_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "SYSTEM.md"
            path.write_text(
                "## Cleanup\nClean the sentence inside <transcript> only.\n", encoding="utf-8"
            )
            self.assertEqual(load_overrides(path), {"cleanup": "Clean the sentence inside <transcript> only."})

    def test_a_cleanup_override_that_drops_the_tags_is_left_out(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "SYSTEM.md"
            path.write_text("## Cleanup\nJust fix the commas.\n", encoding="utf-8")
            self.assertEqual(load_overrides(path), {})

    def test_an_oversized_section_is_left_out(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "SYSTEM.md"
            path.write_text("## Retell\n" + "rule line\n" * 500, encoding="utf-8")
            self.assertEqual(load_overrides(path), {})

    def test_the_retell_prompt_always_keeps_the_marker(self) -> None:
        from jarvis_voice.prompts import NOT_THE_USER

        self.assertTrue(build_retell_prompt({}).startswith(NOT_THE_USER))
        # An override may add rules, never drop the marker that keeps an agent's words out of the user's voice.
        self.assertTrue(build_retell_prompt({"retell": "Say it in ten words."}).startswith(NOT_THE_USER))
        self.assertEqual(
            build_retell_prompt({"retell": f"{NOT_THE_USER} custom]"}), f"{NOT_THE_USER} custom]"
        )


if __name__ == "__main__":
    unittest.main()
