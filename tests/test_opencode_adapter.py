import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from fakes import QUESTION_FORM, make
from jarvis.opencode_adapter import OpenCodeSSEAdapter


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
        get = AsyncMock(side_effect=lambda path: MagicMock(json=MagicMock(return_value={"data": listing[path]})))
        with patch.object(self.adapter._http, "get", new=get):
            await self.adapter._resync()
            await self.adapter._resync()
        self.adapter._surface.assert_awaited_once()
        self.assertEqual(self.adapter._pending_form["id"], "frm_1")

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


if __name__ == "__main__":
    unittest.main()
