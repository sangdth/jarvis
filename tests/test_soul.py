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

    def test_prompt_names_the_connected_thread_only_when_connected(self):
        orchestrator = MagicMock(adapters={})
        self.assertNotIn("connected", system_prompt(orchestrator, ""))
        thread = {"title": "Fix login", "project": "platform-v2", "agent_id": "t3-code"}
        prompt = system_prompt(orchestrator, "", thread)
        self.assertIn('"Fix login" in platform-v2', prompt)
        self.assertIn("send_agent_task for `t3-code`", prompt)

    def test_interview_writes_soul_and_skips_enrollment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SOUL.md"
            answers = iter(["warm, playful", "Sang", tmp, "", "n"])
            with patch.object(soul, "SOUL_PATH", path), patch("builtins.input", lambda _: next(answers)), \
                    patch("jarvis.enroll.main") as enroll, patch("builtins.print"), \
                    patch.object(soul, "copy_config", return_value=[]), patch.object(soul, "ask_api_keys"):
                soul.main()
            enroll.assert_not_called()
            self.assertEqual(load_soul(path).projects_dir, tmp)
            self.assertIn("warm, playful", load_soul(path).personality)

    def test_init_copies_the_defaults_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            self.assertEqual(sorted(p.name for p in soul.copy_config(home)), [".env", "agents.json"])
            self.assertIn("OPENROUTER_API_KEY=", (home / ".env").read_text())
            self.assertIn('"t3-code"', (home / "agents.json").read_text())
            self.assertEqual((home / ".env").stat().st_mode & 0o777, 0o600)
            (home / ".env").write_text("KEY=mine")
            self.assertEqual(soul.copy_config(home), [])  # never overwrites
            self.assertEqual((home / ".env").read_text(), "KEY=mine")

    def test_init_asks_only_for_empty_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("OPENROUTER_API_KEY=sk-or-kept\nELEVENLABS_API_KEY=\nOTHER=1\n")
            with patch.object(soul, "getpass", return_value=" sk_new \n") as ask, patch("builtins.print"):
                soul.ask_api_keys(env)
            ask.assert_called_once()  # OpenRouter is already set
            self.assertEqual(env.read_text(), "OPENROUTER_API_KEY=sk-or-kept\nELEVENLABS_API_KEY=sk_new\nOTHER=1\n")
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)

    def test_init_skips_blank_or_broken_keys_and_adds_missing_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("OTHER=1")
            answers = iter(["", "sk_a b"])  # Enter, then a key with a space that would break the line
            with patch.object(soul, "getpass", lambda _: next(answers)), patch("builtins.print"):
                soul.ask_api_keys(env)
            self.assertEqual(env.read_text(), "OTHER=1")
            with patch.object(soul, "getpass", return_value="sk-or-1"), patch("builtins.print"):
                soul.ask_api_keys(env)
            self.assertEqual(env.read_text(), "OTHER=1\nOPENROUTER_API_KEY=sk-or-1\nELEVENLABS_API_KEY=sk-or-1\n")

if __name__ == "__main__":
    unittest.main()
