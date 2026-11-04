import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis import soul
from jarvis.main import JARVIS_EXECUTIVE_SYSTEM_PROMPT, system_prompt
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

    def test_prompt_adds_personality_only_when_set(self):
        orchestrator = MagicMock(adapters={})
        self.assertNotIn("personality", system_prompt(orchestrator, ""))
        prompt = system_prompt(orchestrator, "Personality: grumpy.")
        self.assertTrue(prompt.startswith(JARVIS_EXECUTIVE_SYSTEM_PROMPT))
        self.assertIn("Personality: grumpy.", prompt)

    def test_interview_writes_soul_and_skips_enrollment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SOUL.md"
            answers = iter(["warm, playful", "Sang", tmp, "", "n"])
            with patch.object(soul, "SOUL_PATH", path), patch("builtins.input", lambda _: next(answers)), \
                    patch("jarvis.enroll.main") as enroll, patch("builtins.print"), \
                    patch.object(soul, "copy_config", return_value=[]):
                soul.main()
            enroll.assert_not_called()
            self.assertEqual(load_soul(path).projects_dir, tmp)
            self.assertIn("warm, playful", load_soul(path).personality)

    def test_init_copies_the_repo_env_and_agents_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, home = Path(tmp) / "repo", Path(tmp) / "home"
            repo.mkdir()
            (repo / ".env").write_text("KEY=real")
            (repo / ".env.example").write_text("KEY=")
            (repo / "agents.json").write_text("{}")
            self.assertEqual(sorted(p.name for p in soul.copy_config(home, repo)), [".env", "agents.json"])
            self.assertEqual((home / ".env").read_text(), "KEY=real")
            self.assertEqual((home / ".env").stat().st_mode & 0o777, 0o600)
            (home / ".env").write_text("KEY=mine")
            self.assertEqual(soul.copy_config(home, repo), [])  # never overwrites
            self.assertEqual((home / ".env").read_text(), "KEY=mine")

    def test_init_falls_back_to_the_example_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, home = Path(tmp) / "repo", Path(tmp) / "home"
            repo.mkdir()
            (repo / ".env.example").write_text("KEY=")
            soul.copy_config(home, repo)
            self.assertEqual((home / ".env").read_text(), "KEY=")
            self.assertFalse((home / "agents.json").exists())


if __name__ == "__main__":
    unittest.main()
