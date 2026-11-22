import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from jarvis import cli


class VersionTest(unittest.TestCase):
    def test_version_is_the_running_checkout(self) -> None:
        with patch.object(cli, "git_version", return_value="master @ abc1234"):
            self.assertEqual(cli.version(), "master @ abc1234")


class LockTest(unittest.TestCase):
    def test_second_jarvis_is_stopped_by_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = cli.hold_lock(Path(tmp))
            with self.assertRaises(SystemExit) as stopped:
                cli.hold_lock(Path(tmp))
            self.assertIn("already running", str(stopped.exception))
            first.close()  # released: the next one starts
            cli.hold_lock(Path(tmp)).close()


class ForegroundTest(unittest.TestCase):
    def _run(self, argv: list[str]):
        run = AsyncMock()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".env").write_text("OPENROUTER_API_KEY=sk-test\n")
            with patch.object(cli.sys, "argv", ["jarvis", *argv]), patch.object(cli, "hold_lock"), \
                    patch.object(cli, "JARVIS_HOME", home), \
                    patch("jarvis.supervisor.prepare_environment"), \
                    patch("jarvis.supervisor.start_all", new=run):
                cli.main()
        return run

    def test_connect_names_what_to_connect_to(self) -> None:
        for argv, target in ((["connect"], "t3"), (["connect", "pi"], "pi"),
                             (["connect", "claude"], "claude"), (["serve", "--connect=opencode"], "opencode")):
            self._run(argv).assert_awaited_once_with(connect=target, meeting=False)

    def test_meeting_flag_and_subcommand_enable_meeting_mode(self) -> None:
        for argv, target in ((["meeting"], None), (["--meeting"], None), (["meeting", "connect"], "t3"),
                             (["connect", "pi", "--meeting"], "pi")):
            self._run(argv).assert_awaited_once_with(connect=target, meeting=True)

    def test_the_old_remote_word_is_a_no_op(self) -> None:
        # The thin client is always served, so `jarvis remote` just listens with this Mac's audio (and a paired Mac's).
        for argv in (["remote"], ["--remote"], ["serve", "--remote"]):
            self._run(argv).assert_awaited_once_with(connect=None, meeting=False)

    def test_a_checkout_without_its_env_stops_before_listening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(cli.sys, "argv", ["jarvis"]), \
                patch.object(cli, "JARVIS_HOME", Path(tmp)), patch.object(cli, "hold_lock") as lock, \
                patch("jarvis.supervisor.prepare_environment"), self.assertRaises(SystemExit) as stopped:
            cli.main()
        self.assertIn("No .env", str(stopped.exception))
        lock.assert_not_called()

    def test_remote_and_meeting_now_go_together(self) -> None:
        # A meeting's call audio is this Mac's, so the old refusal would have blocked driving it from a paired Mac.
        self._run(["remote", "meeting"]).assert_awaited_once_with(connect=None, meeting=True)


class UsageTest(unittest.TestCase):
    def test_help_and_unknown_commands(self) -> None:
        with patch("builtins.print") as printed, patch.object(cli.sys, "argv", ["jarvis", "--help"]):
            cli.main()
        self.assertIn("usage: jarvis", printed.call_args[0][0])
        with patch.object(cli.sys, "argv", ["jarvis", "bogus"]), self.assertRaises(SystemExit) as stopped:
            cli.main()
        self.assertEqual(stopped.exception.code, cli.USAGE)


class HttpTest(unittest.TestCase):
    def test_status_prints_what_the_server_reports(self) -> None:
        payload = {
            "version": "master @ abc1234",
            "pid": 42,
            "uptime_s": 90.0,
            "home": "/repo",
            "mode": "ASSIST",
            "spent_today_usd": 1.5,
            "daily_budget_usd": 200.0,
            "tasks": 3,
            "tasks_ready": 1,
            "tasks_running": 0,
            "skills_active": 2,
            "approvals_waiting": 1,
            "voice": {"state": "listening"},
        }
        with patch.object(cli, "call", return_value=payload) as called, patch("builtins.print") as printed:
            cli.dispatch(["status"])
        called.assert_called_once_with("GET", "/v1/status", url="")
        text = "\n".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("master @ abc1234", text)
        self.assertIn("ASSIST", text)
        self.assertIn("listening", text)

    def test_ask_posts_the_sentence_and_prints_the_answer(self) -> None:
        with patch.object(cli, "call", return_value={"spoken": "Done."}) as called, patch("builtins.print") as printed:
            cli.dispatch(["ask", "check", "my", "calendar"])
        called.assert_called_once_with("POST", "/v1/ask", json={"input": "check my calendar"}, timeout=900.0)
        printed.assert_called_once_with("Done.")

    def test_a_refused_token_explains_itself(self) -> None:
        import httpx

        request = httpx.Request("GET", "http://127.0.0.1:8643/v1/status")
        response = httpx.Response(401, json={"error": "Unauthorized"}, request=request)
        with patch.object(cli.httpx, "Client") as client, self.assertRaises(SystemExit) as stopped:
            client.return_value.__enter__.return_value.request.return_value = response
            cli.call("GET", "/v1/status")
        self.assertIn("refused the token", str(stopped.exception))


class ServiceTest(unittest.TestCase):
    def test_restart_writes_the_plist_and_kicks_launchd(self) -> None:
        with patch.object(cli, "PLIST_PATH", Path("/tmp/com.sangdth.jarvis.plist")), \
                patch.object(cli, "sync_dependencies") as sync, patch.object(cli, "build_cockpit") as build, \
                patch.object(cli, "write_plist") as write, patch.object(cli, "launchd_loaded", return_value=True), \
                patch.object(cli, "launchd_kick") as kick, patch.object(cli, "run_cockpit") as top, \
                patch.object(cli.sys, "platform", "darwin"), patch("builtins.print"):
            cli.service_restart(["--meeting"])
        sync.assert_called_once_with()
        build.assert_called_once_with()
        write.assert_called_once_with(["--meeting"])
        kick.assert_called_once_with()
        top.assert_called_once_with([])

    def test_restart_keeps_the_mode_already_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "p.plist"
            plist.write_text(
                "<array><string>/x/python</string><string>-m</string><string>jarvis.cli</string>"
                "<string>serve</string><string>--meeting</string><string>--connect</string><string>pi</string></array>"
            )
            with patch.object(cli, "PLIST_PATH", plist):
                self.assertEqual(cli.plist_serve_args(), ["--meeting", "--connect", "pi"])

    def test_the_old_remote_flag_is_dropped_from_an_installed_plist(self) -> None:
        # It was its own mode; the thin client is always served now, so `jarvis restart` cleans the plist as it goes.
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "p.plist"
            plist.write_text(
                "<array><string>/x/python</string><string>-m</string><string>jarvis.cli</string>"
                "<string>serve</string><string>--remote</string><string>--meeting</string></array>"
            )
            with patch.object(cli, "PLIST_PATH", plist):
                self.assertEqual(cli.plist_serve_args(), ["--meeting"])

    def test_a_missing_plist_starts_without_a_mode_flag(self) -> None:
        with patch.object(cli, "PLIST_PATH", Path("/tmp/nope-not-here.plist")):
            self.assertEqual(cli.plist_serve_args(), [])

    def test_launch_agent_runs_this_checkout(self) -> None:
        from brain.cli import build_launchd_plist

        plist = build_launchd_plist([])
        self.assertIn("-m", plist)
        self.assertIn("jarvis.cli", plist)
        self.assertIn("<string>serve</string>", plist)
        self.assertNotIn("<string>--remote</string>", plist)
        self.assertIn("WorkingDirectory", plist)


if __name__ == "__main__":
    unittest.main()
