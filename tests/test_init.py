import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import init
from jarvis_core.soul import load_soul
from jarvis_voice.config import Settings


class InitTest(unittest.TestCase):
    def test_interview_writes_soul_and_skips_enrollment(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "shared"
            home.mkdir()
            path = home / "SOUL.md"
            env = home / ".env"
            env.write_text("OPENROUTER_API_KEY=sk-or-1\n")
            answers = iter(["warm, playful", "Sang", tmp, "", "n"])
            with patch.object(init, "SOUL_PATH", path), patch.object(init, "ENV_PATH", env), \
                    patch("builtins.input", lambda _: next(answers)), patch("jarvis_voice.enroll.main") as enroll, \
                    patch("builtins.print"), patch.object(init, "copy_config", return_value=[]), \
                    patch.object(init, "setup_brain", return_value=[]), \
                    patch.object(init, "ask_api_keys"):
                init.main()
            enroll.assert_not_called()
            # Declining the recording records the choice, so the on-by-default master check can still start.
            self.assertIn("MASTER_CHECK=false", env.read_text())
            self.assertEqual(load_soul(path).projects_dir, tmp)
            self.assertIn("warm, playful", load_soul(path).personality)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_accepting_voice_lock_enables_master_check_for_first_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "shared"
            home.mkdir()
            path = home / "SOUL.md"
            env = home / ".env"
            env.write_text("OPENROUTER_API_KEY=sk-or-1\nMASTER_CHECK=false\n")
            answers = iter(["calm", "Sang", tmp, "", "y"])
            with patch.object(init, "SOUL_PATH", path), patch.object(init, "ENV_PATH", env), \
                    patch("builtins.input", lambda _: next(answers)), \
                    patch("builtins.print"), patch.object(init, "copy_config", return_value=[]), \
                    patch.object(init, "setup_brain", return_value=[]), \
                    patch.object(init, "ask_api_keys"):
                init.main()
            self.assertIn("MASTER_CHECK=true", env.read_text())

    def test_enroll_restores_previous_voiceprint_on_cancel_and_enables_master_check_on_save(self):
        from unittest.mock import AsyncMock

        from jarvis_voice import enroll
        from jarvis_voice.speaker import VoiceProfile

        with tempfile.TemporaryDirectory() as tmp:
            vp = Path(tmp) / "owner_voice.npy"
            env = Path(tmp) / ".env"
            vp.write_bytes(b"old-voiceprint")
            env.write_text("OPENROUTER_API_KEY=sk-or-1\nMASTER_CHECK=false\n")

            # Cancelled before speaking: existing voiceprint is restored intact.
            with patch.object(enroll, "VOICEPRINT_PATH", vp), patch.object(enroll, "ENV_PATH", env), \
                    patch.object(enroll.settings, "openrouter_api_key", "sk-or-1"), \
                    patch("jarvis_voice.main.run_pipecat_harness", new=AsyncMock(side_effect=KeyboardInterrupt)), \
                    patch("builtins.print"):
                enroll.main()
            self.assertEqual(vp.read_bytes(), b"old-voiceprint")
            self.assertEqual(vp.stat().st_mode & 0o777, 0o600)

            # Natural chat saves a new voiceprint and flips MASTER_CHECK=true.
            async def fake_chat(*, enroll_owner: bool = False):
                self.assertTrue(enroll_owner)
                vp.write_bytes(b"new-voiceprint")
                return VoiceProfile(id="owner", voiceprint=__import__("numpy").ones(2), name="Sang", clips=2, speech_secs=4.5)

            with patch.object(enroll, "VOICEPRINT_PATH", vp), patch.object(enroll, "ENV_PATH", env), \
                    patch.object(init, "ENV_PATH", env), \
                    patch.object(enroll.settings, "openrouter_api_key", "sk-or-1"), \
                    patch.object(enroll.settings, "master_check", False), \
                    patch("jarvis_voice.main.run_pipecat_harness", side_effect=fake_chat), \
                    patch("builtins.print"):
                enroll.main()
            self.assertEqual(vp.read_bytes(), b"new-voiceprint")
            self.assertIn("MASTER_CHECK=true", env.read_text())

    def test_init_copies_the_defaults_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            self.assertEqual(sorted(p.name for p in init.copy_config(home)), [".env", "agents.json"])
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)
            self.assertIn("OPENROUTER_API_KEY=", (home / ".env").read_text())
            self.assertIn('"brain"', (home / "agents.json").read_text())
            self.assertEqual((home / ".env").stat().st_mode & 0o777, 0o600)
            (home / ".env").write_text("KEY=mine")
            self.assertEqual(init.copy_config(home), [])  # never overwrites
            self.assertEqual((home / ".env").read_text(), "KEY=mine")

    def test_init_asks_only_for_empty_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("OPENROUTER_API_KEY=sk-or-kept\nELEVENLABS_API_KEY=\nOTHER=1\n")
            with patch.object(init, "getpass", return_value=" sk_new \n") as ask, patch("builtins.print"), \
                    patch.dict(os.environ, {}, clear=True):
                init.ask_api_keys(env)
            ask.assert_called_once()  # OpenRouter is already set
            self.assertEqual(env.read_text(), "OPENROUTER_API_KEY=sk-or-kept\nELEVENLABS_API_KEY=sk_new\nOTHER=1\n")
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)

    def test_init_skips_blank_or_broken_keys_and_adds_missing_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("OTHER=1")
            answers = iter(["", "sk_a b"])  # Enter, then a key with a space that would break the line
            with patch.object(init, "getpass", lambda _: next(answers)), patch("builtins.print"), \
                    patch.dict(os.environ, {}, clear=True):
                init.ask_api_keys(env)
            self.assertEqual(env.read_text(), "OTHER=1")
            with patch.object(init, "getpass", return_value="sk-or-1"), patch("builtins.print"), \
                    patch.dict(os.environ, {}, clear=True):
                init.ask_api_keys(env)
            self.assertEqual(env.read_text(), "OTHER=1\nOPENROUTER_API_KEY=sk-or-1\nELEVENLABS_API_KEY=sk-or-1\n")

    def test_init_uses_keys_from_the_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("OPENROUTER_API_KEY=\nELEVENLABS_API_KEY=\n")
            with patch.object(init, "getpass") as ask, patch("builtins.print"), \
                    patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-shell", "ELEVENLABS_API_KEY": "sk_shell"}):
                init.ask_api_keys(env)
                self.assertEqual(Settings(_env_file=env).openrouter_api_key, "sk-or-shell")  # shell beats blank .env
            ask.assert_not_called()
            self.assertEqual(env.read_text(), "OPENROUTER_API_KEY=\nELEVENLABS_API_KEY=\n")  # no copy on disk

    def test_settings_falls_back_to_env_for_empty_or_padded_shell_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("OPENROUTER_API_KEY=sk-or-file\n")
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
                self.assertEqual(Settings(_env_file=env).openrouter_api_key, "sk-or-file")
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": " sk-or-shell\n"}):
                self.assertEqual(Settings(_env_file=env).openrouter_api_key, "sk-or-shell")

    def test_set_env_replaces_a_line_or_adds_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("MASTER_CHECK=false\nOTHER=1\n")
            init.set_env("MASTER_CHECK", "true", env)
            init.set_env("MEETING_GUESTS", "allow", env)
            self.assertEqual(env.read_text(), "MASTER_CHECK=true\nOTHER=1\nMEETING_GUESTS=allow\n")
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
