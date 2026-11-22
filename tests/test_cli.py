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
        with patch("jarvis_core.home.distribution", return_value=dist(url)):
            self.assertEqual(cli.version(), "master @ 0fbc724")

    def test_version_of_a_checkout_is_dev_with_its_folder(self) -> None:
        url = {"url": "file:///Users/me/jarvis", "dir_info": {"editable": True}}
        with patch("jarvis_core.home.distribution", return_value=dist(url)):
            self.assertEqual(cli.version(), "dev (/Users/me/jarvis)")

    def test_update_of_a_dev_checkout_fast_forwards_the_checkout(self) -> None:
        with patch.object(cli, "version", return_value="dev (/x)"), patch.object(cli.subprocess, "run") as run, \
                patch.object(cli, "upgrade_checkout") as upgrade:
            cli.update_installed()
        run.assert_not_called()
        upgrade.assert_called_once_with()

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

    def test_uninstall_deletes_the_folder_then_the_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "jarvis"
            home.mkdir()
            (home / ".env").write_text("OPENROUTER_API_KEY=sk-or-1")
            with patch.object(cli, "version", return_value="master @ abc1234"), \
                    patch.object(cli.shutil, "which", return_value="/bin/uv"), patch("builtins.print"), \
                    patch("builtins.input", return_value="y"), patch.object(cli.subprocess, "run") as run:
                run.return_value.returncode = 0
                cli.uninstall(home)
            self.assertFalse(home.exists())
            run.assert_called_once_with(["/bin/uv", "tool", "uninstall", cli.PACKAGE])

    def test_uninstall_keeps_everything_unless_told_yes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch.object(cli, "version", return_value="master @ abc1234"), \
                    patch.object(cli.shutil, "which", return_value="/bin/uv"), patch("builtins.print"), \
                    patch("builtins.input", return_value=""), patch.object(cli.subprocess, "run") as run:
                cli.uninstall(home)
            self.assertTrue(home.exists())
            run.assert_not_called()

    def test_uninstall_refuses_while_jarvis_listens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            listening = cli.hold_lock(Path(tmp))
            with patch.object(cli, "version", return_value="master @ abc1234"), \
                    patch.object(cli.shutil, "which", return_value="/bin/uv"), \
                    patch("builtins.input") as ask, self.assertRaises(SystemExit):
                cli.uninstall(Path(tmp))
            ask.assert_not_called()
            self.assertTrue(Path(tmp).exists())
            listening.close()

    def test_uninstall_from_a_checkout_keeps_the_uv_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "jarvis"
            home.mkdir()
            with patch.object(cli, "version", return_value="dev (/x)"), patch("builtins.print"), \
                    patch("builtins.input", return_value="y"), patch.object(cli.subprocess, "run") as run:
                cli.uninstall(home)
            self.assertFalse(home.exists())
            run.assert_not_called()

    @staticmethod
    def _attached():
        """The brain side of `jarvis`: attach to a daemon that is already up, so no token files or ports."""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def running():
            yield False

        return running

    def _listen(self, argv: list[str]):
        """Run `jarvis` with the voice pipeline and the brain faked; returns the pipeline mock."""
        from contextlib import ExitStack
        from unittest.mock import AsyncMock

        run = AsyncMock()
        with ExitStack() as stack:
            stack.enter_context(patch.object(cli.sys, "argv", argv))
            stack.enter_context(patch.object(cli, "checkout_root", return_value=None))
            stack.enter_context(patch.object(cli, "hold_lock"))
            stack.enter_context(patch.object(cli, "prepare_brain"))
            stack.enter_context(patch("jarvis.supervisor.superbrain_running", new=self._attached()))
            stack.enter_context(patch("jarvis_voice.main.run_pipecat_harness", new=run))
            cli.main()
        return run

    def test_connect_names_what_to_connect_to(self) -> None:
        for argv, target in ((["jarvis"], None), (["jarvis", "connect"], "t3"), (["jarvis", "connect", "pi"], "pi"),
                            (["jarvis", "connect", "claude"], "claude")):
            self._listen(argv).assert_awaited_once_with(connect=target, meeting=False, remote=False)

    def test_meeting_flag_and_subcommand_enable_meeting_mode(self) -> None:
        for argv, target in (
            (["jarvis", "meeting"], None),
            (["jarvis", "--meeting"], None),
            (["jarvis", "meeting", "connect"], "t3"),
            (["jarvis", "connect", "pi", "--meeting"], "pi"),
        ):
            self._listen(argv).assert_awaited_once_with(connect=target, meeting=True, remote=False)

    def test_remote_subcommand_listens_for_a_relay_client(self) -> None:
        for argv, target in ((["jarvis", "remote"], None), (["jarvis", "remote", "connect"], "t3")):
            self._listen(argv).assert_awaited_once_with(connect=target, meeting=False, remote=True)

    def test_remote_and_meeting_together_are_refused(self) -> None:
        with patch.object(cli.sys, "argv", ["jarvis", "remote", "meeting"]), self.assertRaises(SystemExit) as stopped:
            cli.main()
        self.assertEqual(stopped.exception.code, cli.USAGE)

    def test_unknown_command_prints_usage(self) -> None:
        for argv in (["jarvis", "bogus"], ["jarvis", "connect", "claude", "extra"]):
            with patch.object(cli.sys, "argv", argv), self.assertRaises(SystemExit) as stopped:
                cli.main()
            self.assertEqual(stopped.exception.code, cli.USAGE)


if __name__ == "__main__":
    unittest.main()
