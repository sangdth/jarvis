import tempfile
import unittest
from pathlib import Path

from jarvis.system_prompts import load_overrides, parse_overrides


class TestSystemPrompts(unittest.TestCase):
    def test_no_file_means_no_overrides(self) -> None:
        self.assertEqual(load_overrides(Path(tempfile.gettempdir()) / "no-such-SYSTEM.md"), {})

    def test_a_cleanup_section_overrides_and_its_name_ignores_case(self) -> None:
        text = "# My prompts\nintro text\n\n## CleanUp\nFix punctuation only.\nKeep fillers.\n"
        self.assertEqual(parse_overrides(text), {"cleanup": "Fix punctuation only.\nKeep fillers."})

    def test_empty_and_unknown_sections_are_left_out(self) -> None:
        self.assertEqual(parse_overrides("## Cleanup\n\n## Cleanp\nTypo.\n"), {})

    def test_reads_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "SYSTEM.md"
            path.write_text("## Cleanup\nOnly commas.\n", encoding="utf-8")
            self.assertEqual(load_overrides(path), {"cleanup": "Only commas."})


if __name__ == "__main__":
    unittest.main()
