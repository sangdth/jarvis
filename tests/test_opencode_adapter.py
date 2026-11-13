import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from fakes import QUESTION_FORM, make
from jarvis.opencode_adapter import OpenCodeSSEAdapter, discover_service


class TestOpenCodeAdapter(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.adapter = make(OpenCodeSSEAdapter, "opencode_sse")
        self.post = AsyncMock(return_value=MagicMock(is_success=True))

    async def asyncTearDown(self) -> None:
        await self.adapter._http.aclose()

    async def test_permission_reply_goes_to_permission_endpoint(self) -> None:
        event = {
            "type": "permission.asked",
            "data": {"id": "per_1", "sessionID": "ses_1", "action": "read", "resources": ["/etc/hosts"]},
        }
        await self.adapter._handle_event(event)
        self.adapter._surface.assert_awaited_once_with(
            "needs approval: read /etc/hosts"
        )

        failing = AsyncMock(side_effect=httpx.ConnectError("down"))
        with patch.object(self.adapter._http, "post", new=failing):
            self.assertFalse(await self.adapter.answer("yes"))
        self.assertIsNotNone(self.adapter._pending_permission)  # still answerable after the network error

        with patch.object(self.adapter._http, "post", new=self.post):
            self.assertTrue(await self.adapter.answer("yes, go ahead"))
        self.post.assert_awaited_once_with("/api/session/ses_1/permission/per_1/reply", json={"decision": "once"})
        self.adapter.state.active_agent_context.pop.assert_called_with("a1", None)  # speech check forgets it

    async def test_question_form_is_read_out_and_answered(self) -> None:
        # Shape captured from a live OpenCode v2.2.5 server: the form sits under data.form
        await self.adapter._handle_event({"type": "form.created", "data": {"form": QUESTION_FORM}})
        self.adapter._surface.assert_awaited_once_with(
            "asks: Which should come next: tests or docs? Options: Tests, Docs."
        )
        with patch.object(self.adapter._http, "post", new=self.post):
            self.assertTrue(await self.adapter.answer("Tests first"))
        self.post.assert_awaited_once_with("/api/session/ses_1/form/frm_1/reply", json={"answer": {"q0": "Tests"}})

    async def test_finished_turn_is_surfaced_and_next_message_is_a_prompt(self) -> None:
        ended = {"type": "session.text.ended", "data": {"sessionID": "ses_1", "text": "All tests pass."}}
        await self.adapter._handle_event(ended)
        await self.adapter._handle_event({"type": "session.execution.succeeded", "data": {"sessionID": "ses_1"}})
        self.adapter._surface.assert_awaited_once_with("All tests pass.", is_report=True)

        with patch.object(self.adapter._http, "post", new=self.post):
            self.assertFalse(await self.adapter.answer("Now add dark mode"))
            await self.adapter.send_task("Now add dark mode")
        self.post.assert_awaited_once_with("/api/session/ses_1/prompt", json={"text": "Now add dark mode"})

    async def test_sessions_are_listed_newest_first_and_scoped_to_a_folder(self) -> None:
        listing = {"data": [
            {"id": "ses_old", "title": "Old", "location": {"directory": "/p/a"}, "time": {"updated": 1_000}},
            {"id": "ses_new", "title": "", "location": {"directory": "/p/a"}, "time": {"updated": 5_000}},
        ]}
        get = AsyncMock(return_value=MagicMock(json=lambda: listing, raise_for_status=lambda: None))
        no_service = patch("jarvis.opencode_adapter.discover_service", return_value=None)
        with patch.object(self.adapter._http, "get", new=get), no_service:
            sessions = await self.adapter.list_sessions("/p/a", limit=1)
        get.assert_awaited_once_with("/api/session", headers={"x-opencode-directory": "/p/a"})
        self.assertEqual([(s.id, s.title, s.updated_at) for s in sessions], [("ses_new", "(untitled)", 5.0)])

    async def test_pick_moves_to_the_session_and_forgets_the_old_question(self) -> None:
        from jarvis.sessions import AgentSession

        self.adapter._session_id, self.adapter._directory = "ses_1", "/p/a"
        self.adapter._pending_permission = {"id": "per_1", "sessionID": "ses_1"}
        empty = MagicMock(json=lambda: {"data": []}, raise_for_status=lambda: None)
        with patch.object(self.adapter._http, "get", new=AsyncMock(return_value=empty)) as get:
            self.assertTrue(await self.adapter.pick_session(AgentSession("ses_2", "Login", "/p/b", 1.0), connected=True))
        self.assertEqual((self.adapter._session_id, self.adapter._directory), ("ses_2", "/p/b"))
        self.assertIsNone(self.adapter._pending_permission)  # asked in the old session: can't be answered from here
        self.assertTrue(self.adapter.connected)
        get.assert_any_await("/api/permission/request", headers={"x-opencode-directory": "/p/b"})  # resynced there
        self.adapter._running = True
        self.assertFalse(await self.adapter.pick_session(AgentSession("ses_3", "x", "/p/b", 1.0)))  # not mid-turn

    async def test_new_session_is_made_in_the_folder_and_a_failure_never_reuses_the_old_one(self) -> None:
        self.adapter._session_id, self.adapter._directory = "ses_1", "/p/a"
        made = MagicMock(json=lambda: {"data": {"id": "ses_9"}}, raise_for_status=lambda: None)
        with patch.object(self.adapter._http, "post", new=AsyncMock(return_value=made)) as post:
            self.assertTrue(await self.adapter.new_session("/p/b"))
        post.assert_awaited_once_with("/api/session", json={"title": "Jarvis", "location": {"directory": "/p/b"}})
        self.assertEqual(self.adapter._session_id, "ses_9")
        with patch.object(self.adapter._http, "post", new=AsyncMock(side_effect=httpx.ConnectError("down"))):
            self.assertFalse(await self.adapter.new_session())
        self.assertIsNone(self.adapter._session_id)  # the next task makes one instead of going to ses_9

    async def test_a_sub_agent_session_does_not_end_the_turn(self) -> None:
        self.adapter._session_id, self.adapter._running = "ses_1", True
        await self.adapter._handle_event({"type": "session.text.ended", "data": {"sessionID": "ses_child", "text": "x"}})
        await self.adapter._handle_event({"type": "session.execution.succeeded", "data": {"sessionID": "ses_child"}})
        self.adapter._surface.assert_not_awaited()
        self.assertTrue(self.adapter.busy)
        self.assertEqual(self.adapter._session_id, "ses_1")  # the next task still goes to the main session

    async def test_form_answered_elsewhere_is_no_longer_pending(self) -> None:
        await self.adapter._handle_event({"type": "form.created", "data": {"form": QUESTION_FORM}})
        await self.adapter._handle_event({"type": "form.replied", "data": {"id": "frm_1", "sessionID": "ses_1"}})
        with patch.object(self.adapter._http, "post", new=self.post):
            self.assertFalse(await self.adapter.answer("Tests"))
        self.post.assert_not_awaited()

    async def test_first_task_creates_a_session(self) -> None:
        created = MagicMock(is_success=True, json=MagicMock(return_value={"data": {"id": "ses_new"}}))
        self.post.side_effect = [created, MagicMock(is_success=True)]
        with patch.object(self.adapter._http, "post", new=self.post):
            self.assertTrue(await self.adapter.send_task("build a navbar"))
        self.assertEqual(self.post.await_args_list[0].args[0], "/api/session")
        self.post.assert_awaited_with("/api/session/ses_new/prompt", json={"text": "build a navbar"})

    async def test_resync_reads_out_each_pending_request_once(self) -> None:
        listing = {"/api/permission/request": [], "/api/form": [QUESTION_FORM]}
        get = AsyncMock(side_effect=lambda path, **_: MagicMock(json=MagicMock(return_value={"data": listing[path]})))
        with patch.object(self.adapter._http, "get", new=get):
            await self.adapter._resync()
            await self.adapter._resync()
        self.adapter._surface.assert_awaited_once()
        self.assertEqual(self.adapter._pending_form["id"], "frm_1")

    async def test_failed_turn_with_empty_text_still_surfaces(self) -> None:
        self.adapter._session_id, self.adapter._running = "ses_1", True
        await self.adapter._handle_event({"type": "session.execution.failed", "data": {"sessionID": "ses_1"}})
        self.assertFalse(self.adapter.busy)
        self.adapter._surface.assert_awaited_once_with("The turn failed.", is_report=True)

    async def test_resync_clears_stale_pending_requests_and_idle_running(self) -> None:
        self.adapter._session_id = "ses_1"
        self.adapter._running = True
        self.adapter._pending_permission = {"id": "per_old", "sessionID": "ses_1"}
        self.adapter._pending_form = {"id": "frm_old", "sessionID": "ses_1"}
        responses = {
            "/api/permission/request": {"data": []},
            "/api/form": {"data": []},
            "/api/session/ses_1": {"data": {"time": {"idle": True}}},
        }
        get = AsyncMock(side_effect=lambda path, **_: MagicMock(json=MagicMock(return_value=responses[path])))
        with patch.object(self.adapter._http, "get", new=get):
            await self.adapter._resync()
        self.assertIsNone(self.adapter._pending_permission)
        self.assertIsNone(self.adapter._pending_form)
        self.assertFalse(self.adapter.busy)
        self.assertEqual(self.adapter.state.active_agent_context.pop.call_count, 2)

    async def test_stop_interrupts_the_session(self) -> None:
        self.adapter._session_id = "ses_1"
        with patch.object(self.adapter._http, "post", new=self.post):
            self.assertTrue(await self.adapter.interrupt())
        self.post.assert_awaited_once_with("/api/session/ses_1/interrupt")

    async def test_stop_then_send_keeps_the_new_task_running(self) -> None:
        self.adapter._session_id = "ses_1"
        with patch.object(self.adapter._http, "post", new=self.post):
            await self.adapter.send_task("look up Hacker Hustle")
            await self.adapter.interrupt()
            await self.adapter.send_task("look up HuggerHustle")
        await self.adapter._handle_event({"type": "session.execution.interrupted", "data": {"sessionID": "ses_1"}})
        self.assertTrue(self.adapter.busy)
        # Stopped in the OpenCode UI instead: the turn is over.
        await self.adapter._handle_event({"type": "session.execution.interrupted", "data": {"sessionID": "ses_1"}})
        self.assertFalse(self.adapter.busy)

    async def test_a_repeated_connection_failure_is_logged_once(self) -> None:
        refused = httpx.ConnectError("All connection attempts failed")
        errors = [refused, refused, refused, httpx.ReadTimeout("silent"), refused]
        sleep = AsyncMock(side_effect=[None] * (len(errors) - 1) + [asyncio.CancelledError()])
        with (
            patch.object(self.adapter._http, "stream", side_effect=errors),
            patch("jarvis.opencode_adapter.asyncio.sleep", new=sleep),
            patch("jarvis.opencode_adapter.logger") as logger,
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.adapter._run_loop()
        logged = [call.args[0] for call in logger.debug.call_args_list]
        self.assertEqual(len(logged), 3, logged)  # refused, then the timeout, then refused again
        self.assertIn("ReadTimeout", logged[1])


class TestDiscoverService(unittest.TestCase):
    """The registered OpenCode service is used only when the file is the user's own and names a loopback url."""

    def setUp(self) -> None:
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        self.file = Path(self.state.name) / "opencode" / "service.json"
        self.file.parent.mkdir()
        patcher = patch.dict(os.environ, {"XDG_STATE_HOME": self.state.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, url: str = "http://127.0.0.1:49374/", mode: int = 0o600, pid: int | None = None) -> None:
        pid = os.getpid() if pid is None else pid  # a live process, so the file counts as current
        self.file.write_text(json.dumps({"id": "x", "url": url, "pid": pid, "password": "pw"}))
        self.file.chmod(mode)

    def test_owner_only_loopback_file_is_used(self) -> None:
        self.write()
        self.assertEqual(discover_service(), ("http://127.0.0.1:49374", "pw"))

    def test_file_of_a_dead_process_is_ignored(self) -> None:
        with patch("jarvis.opencode_adapter.os.kill", side_effect=ProcessLookupError):
            self.write()
            self.assertIsNone(discover_service())

    def test_process_of_another_user_still_counts(self) -> None:
        with patch("jarvis.opencode_adapter.os.kill", side_effect=PermissionError):
            self.write()
            self.assertEqual(discover_service(), ("http://127.0.0.1:49374", "pw"))

    def test_unreadable_file_is_ignored(self) -> None:
        self.write(mode=0o000)
        self.assertIsNone(discover_service())

    def test_no_file(self) -> None:
        self.assertIsNone(discover_service())

    def test_readable_by_others_is_ignored(self) -> None:
        self.write(mode=0o644)
        self.assertIsNone(discover_service())

    def test_remote_url_is_ignored(self) -> None:
        self.write(url="http://evil.example:80")
        self.assertIsNone(discover_service())

    def test_symlink_is_not_followed(self) -> None:
        real = Path(self.state.name) / "real.json"
        real.write_text(json.dumps({"url": "http://127.0.0.1:1", "password": "pw"}))
        real.chmod(0o600)
        self.file.symlink_to(real)
        self.assertIsNone(discover_service())


class TestSharedService(unittest.IsolatedAsyncioTestCase):
    """On the shared service Jarvis keeps to the sessions in its own folder."""

    def setUp(self) -> None:
        with patch("jarvis.opencode_adapter.settings") as settings:  # not pinned, whatever the local .env says
            settings.opencode_server_password = ""
            self.adapter = make(OpenCodeSSEAdapter, "opencode_sse")

    async def asyncTearDown(self) -> None:
        await self.adapter._http.aclose()

    def use_service(self) -> None:
        with patch("jarvis.opencode_adapter.discover_service", return_value=("http://127.0.0.1:49374", "pw")):
            self.adapter._use_service()

    @staticmethod
    def session_in(folder: str) -> AsyncMock:
        return AsyncMock(return_value=MagicMock(
            raise_for_status=MagicMock(), json=MagicMock(return_value={"data": {"location": {"directory": folder}}})
        ))

    def test_found_service_replaces_the_default_url(self) -> None:
        self.use_service()
        self.assertEqual(self.adapter.base_url, "http://127.0.0.1:49374")
        self.assertEqual(str(self.adapter._http.base_url).rstrip("/"), "http://127.0.0.1:49374")
        self.assertEqual(self.adapter._directory, str(Path("/tmp").resolve()))

    def test_a_set_password_pins_the_server(self) -> None:
        with patch("jarvis.opencode_adapter.settings") as settings:
            settings.opencode_server_password = "mine"
            adapter = make(OpenCodeSSEAdapter, "opencode_sse")
        with patch("jarvis.opencode_adapter.discover_service", return_value=("http://127.0.0.1:1", "pw")) as found:
            adapter._use_service()
        found.assert_not_called()
        self.assertEqual(adapter.base_url, "http://127.0.0.1:4096")

    async def test_another_folders_request_is_not_read_out(self) -> None:
        self.use_service()
        event = {"type": "permission.asked", "data": {"id": "per_1", "sessionID": "ses_x", "action": "bash"}}
        get = self.session_in("/Users/someone/other-project")
        with patch.object(self.adapter._http, "get", new=get):
            await self.adapter._handle_event({"type": "message.part.updated", "data": {"sessionID": "ses_noise"}})
            get.assert_not_awaited()  # unhandled event types never look up the session
            await self.adapter._handle_event(event)
            await self.adapter._handle_event({"type": "session.text.ended", "data": {"sessionID": "ses_x"}})
        get.assert_awaited_once_with("/api/session/ses_x")  # looked up once, then remembered
        self.adapter._surface.assert_not_awaited()
        self.assertIsNone(self.adapter._pending_permission)
        self.assertIsNone(self.adapter._session_id)

    async def test_own_folders_session_is_adopted(self) -> None:
        self.use_service()
        event = {"type": "permission.asked", "data": {"id": "per_1", "sessionID": "ses_1", "action": "bash"}}
        with patch.object(self.adapter._http, "get", new=self.session_in(str(Path("/tmp").resolve()))):
            await self.adapter._handle_event(event)
        self.adapter._surface.assert_awaited_once()
        self.assertEqual(self.adapter._session_id, "ses_1")

    async def test_failed_lookup_is_retried_later(self) -> None:
        self.use_service()
        failing = AsyncMock(side_effect=httpx.ConnectError("down"))
        with patch.object(self.adapter._http, "get", new=failing):
            self.assertFalse(await self.adapter._is_ours("ses_1"))
        with patch.object(self.adapter._http, "get", new=self.session_in(str(Path("/tmp").resolve()))):
            self.assertTrue(await self.adapter._is_ours("ses_1"))

    async def test_new_session_and_resync_are_scoped_to_cwd(self) -> None:
        self.use_service()
        folder = str(Path("/tmp").resolve())
        created = MagicMock(is_success=True, json=MagicMock(return_value={"data": {"id": "ses_new"}}))
        post = AsyncMock(side_effect=[created, MagicMock(is_success=True)])
        with patch.object(self.adapter._http, "post", new=post):
            self.assertTrue(await self.adapter.send_task("build a navbar"))
        self.assertEqual(post.await_args_list[0].kwargs["json"]["location"], {"directory": folder})
        get = AsyncMock(return_value=MagicMock(json=MagicMock(return_value={"data": []})))
        with patch.object(self.adapter._http, "get", new=get):
            await self.adapter._resync()
        for call in get.await_args_list:
            self.assertEqual(call.kwargs["headers"], {"x-opencode-directory": folder})


if __name__ == "__main__":
    unittest.main()
