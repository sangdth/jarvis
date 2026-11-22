import unittest
from pathlib import Path

from jarvis_core.home import JARVIS_HOME, REPO_ROOT, resolve_home
from jarvis_voice.config import WHISPER_MODELS, Settings


class ConfigTest(unittest.TestCase):
    def test_env_file_is_in_jarvis_home_not_the_current_folder(self) -> None:
        self.assertEqual(Settings.model_config["env_file"], JARVIS_HOME / ".env")

    def test_the_checkout_is_the_runtime_and_the_default_state_home(self) -> None:
        # No wheel/install split: the code that runs is this checkout, and its state lives beside it.
        self.assertIn("[tool.uv.workspace]", (REPO_ROOT / "pyproject.toml").read_text())
        self.assertEqual(JARVIS_HOME, REPO_ROOT)

    def test_jarvis_home_can_be_pointed_somewhere_else(self) -> None:
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"JARVIS_HOME": "/tmp/jarvis-home-test"}):
            self.assertEqual(resolve_home(), Path("/tmp/jarvis-home-test").resolve())

    def test_agents_json_path_counts_from_jarvis_home(self) -> None:
        self.assertEqual(Settings(_env_file=None).watch_config_path, str(JARVIS_HOME / "agents.json"))
        self.assertEqual(Settings(_env_file=None, watch_config_path="./agents.json").watch_config_path,
                         str(JARVIS_HOME / "agents.json"))
        self.assertEqual(Settings(_env_file=None, watch_config_path="/etc/a.json").watch_config_path, "/etc/a.json")

    def test_language_picks_the_whisper_model_unless_one_is_named(self) -> None:
        self.assertEqual(Settings(_env_file=None).whisper_model, WHISPER_MODELS["en"])
        self.assertEqual(Settings(_env_file=None, language="vi").whisper_model, "sangdth/phowhisper-large-vien-cs-mlx-16bit")
        self.assertEqual(Settings(_env_file=None, language="vi", whisper_model="/models/x").whisper_model, "/models/x")
        with self.assertRaises(ValueError):
            Settings(_env_file=None, language="en_US:en")  # the locale variable LANGUAGE stops Jarvis, never guesses

    def test_a_wildcard_bind_address_is_reached_over_loopback(self) -> None:
        default = Settings(_env_file=None)
        self.assertEqual(default.brain_host, "0.0.0.0")
        self.assertEqual(default.connect_host, "127.0.0.1")
        self.assertEqual(default.endpoint, "http://127.0.0.1:8643")
        pinned = Settings(_env_file=None, brain_host="mac-mini.local", brain_port=9000)
        self.assertEqual(pinned.endpoint, "http://mac-mini.local:9000")
        self.assertEqual(Settings(_env_file=None, brain_url="http://elsewhere:1").endpoint, "http://elsewhere:1")


if __name__ == "__main__":
    unittest.main()
