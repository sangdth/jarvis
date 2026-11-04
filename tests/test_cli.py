import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis import cli


def dist(direct_url: dict | None) -> MagicMock:
    d = MagicMock()
    d.read_text.return_value = json.dumps(direct_url) if direct_url is not None else None
    return d


class CliTest(unittest.TestCase):
    def test_version_of_a_git_install_names_branch_and_commit(self) -> None:
        url = {"url": "https://github.com/sangdth/jarvis", "vcs_info": {"vcs": "git", "requested_revision": "master",
                                                                       "commit_id": "0fbc724abcdef"}}
        with patch("jarvis.config.distribution", return_value=dist(url)):
            self.assertEqual(cli.version(), "master @ 0fbc724")

    def test_version_of_a_checkout_is_dev_with_its_folder(self) -> None:
        url = {"url": "file:///Users/me/jarvis", "dir_info": {"editable": True}}
        with patch("jarvis.config.distribution", return_value=dist(url)):
            self.assertEqual(cli.version(), "dev (/Users/me/jarvis)")

    def test_update_refuses_a_dev_checkout(self) -> None:
        with patch.object(cli, "version", return_value="dev (/x)"), patch.object(cli.subprocess, "run") as run:
            with self.assertRaises(SystemExit):
                cli.update()
            run.assert_not_called()

    def test_second_jarvis_is_stopped_by_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = cli.hold_lock(Path(tmp))
            with self.assertRaises(SystemExit) as stopped:
                cli.hold_lock(Path(tmp))
            self.assertIn("already running", str(stopped.exception))
            first.close()  # released: the next one starts
            cli.hold_lock(Path(tmp)).close()

    def test_dev_checkout_without_its_env_stops_before_listening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(cli.sys, "argv", ["jarvis"]), \
                patch.object(cli, "checkout_root", return_value=Path(tmp)), patch.object(cli, "JARVIS_HOME", Path(tmp)), \
                patch.object(cli, "hold_lock") as lock, self.assertRaises(SystemExit) as stopped:
            cli.main()
        self.assertIn("No .env", str(stopped.exception))
        lock.assert_not_called()

    def test_unknown_command_prints_usage(self) -> None:
        with patch.object(cli.sys, "argv", ["jarvis", "bogus"]), self.assertRaises(SystemExit) as stopped:
            cli.main()
        self.assertEqual(stopped.exception.code, cli.USAGE)


if __name__ == "__main__":
    unittest.main()
