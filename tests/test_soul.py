import unittest
from pathlib import Path

from jarvis.soul import Soul, load_soul, parse_soul, render_soul


class SoulTest(unittest.TestCase):
    def test_missing_file_gives_defaults(self):
        self.assertEqual(load_soul(Path("/nonexistent/SOUL.md")), Soul())

    def test_render_then_parse_round_trips(self):
        text = render_soul("calm, dry wit", "Sang", "~/Code", "Short answers.")
        self.assertEqual(
            parse_soul(text),
            Soul(personality='Personality: calm, dry wit.\nCall the user "Sang".\nShort answers.', projects_dir="~/Code"),
        )

    def test_plain_text_without_front_matter_is_all_personality(self):
        self.assertEqual(parse_soul("Be warm.\n"), Soul(personality="Be warm."))


if __name__ == "__main__":
    unittest.main()
