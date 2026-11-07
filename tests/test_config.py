import unittest
from pathlib import Path

from jarvis.config import JARVIS_HOME, SHARED_HOME, WHISPER_MODELS, Settings, checkout_root


class ConfigTest(unittest.TestCase):
    def test_env_file_is_in_jarvis_home_not_the_current_folder(self) -> None:
        self.assertEqual(Settings.model_config["env_file"], JARVIS_HOME / ".env")

    def test_installed_jarvis_lives_in_the_shared_home(self) -> None:
        git = {"url": "https://github.com/sangdth/jarvis", "vcs_info": {"vcs": "git", "commit_id": "0fbc724"}}
        self.assertIsNone(checkout_root(git))
        self.assertEqual(checkout_root(git) or SHARED_HOME, SHARED_HOME)

    def test_dev_checkout_lives_in_its_own_folder_not_the_current_one(self) -> None:
        self.assertEqual(checkout_root({"url": "file:///Users/me/wt", "dir_info": {"editable": True}}), Path("/Users/me/wt"))
        # No install metadata (tests on PYTHONPATH=src): the folder holding this code's src/
        self.assertEqual(checkout_root({}), Path(__file__).resolve().parents[1])

    def test_agents_json_path_counts_from_jarvis_home(self) -> None:
        self.assertEqual(Settings(_env_file=None).watch_config_path, str(JARVIS_HOME / "agents.json"))
        self.assertEqual(Settings(_env_file=None, watch_config_path="./agents.json").watch_config_path,
                         str(JARVIS_HOME / "agents.json"))
        self.assertEqual(Settings(_env_file=None, watch_config_path="/etc/a.json").watch_config_path, "/etc/a.json")

    def test_language_picks_the_whisper_model_unless_one_is_named(self) -> None:
        self.assertEqual(Settings(_env_file=None).whisper_model, WHISPER_MODELS["en"])
        self.assertEqual(Settings(_env_file=None, language="vi").whisper_model, "sangdth/phowhisper-large-vien-cs-mlx")
        self.assertEqual(Settings(_env_file=None, language="vi", whisper_model="/models/x").whisper_model, "/models/x")
        with self.assertRaises(ValueError):
            Settings(_env_file=None, language="en_US:en")  # the locale variable LANGUAGE stops Jarvis, never guesses


if __name__ == "__main__":
    unittest.main()
