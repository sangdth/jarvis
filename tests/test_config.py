import unittest

from jarvis.config import JARVIS_HOME, Settings


class ConfigTest(unittest.TestCase):
    def test_env_file_is_in_jarvis_home_not_the_current_folder(self) -> None:
        self.assertEqual(Settings.model_config["env_file"], JARVIS_HOME / ".env")

    def test_agents_json_path_counts_from_jarvis_home(self) -> None:
        self.assertEqual(Settings(_env_file=None).watch_config_path, str(JARVIS_HOME / "agents.json"))
        self.assertEqual(Settings(_env_file=None, watch_config_path="./agents.json").watch_config_path,
                         str(JARVIS_HOME / "agents.json"))
        self.assertEqual(Settings(_env_file=None, watch_config_path="/etc/a.json").watch_config_path, "/etc/a.json")


if __name__ == "__main__":
    unittest.main()
