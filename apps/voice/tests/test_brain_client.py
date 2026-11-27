"""The voice's one client: what it sends to the brain, and how it carries a question back."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from jarvis_voice.agent_adapter import WatchedAgentConfig
from jarvis_voice.brain_client import BrainClientAdapter
from jarvis_voice.sessions import AgentSession


class _Response:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    @property
    def is_success(self) -> bool:
        return self.status_code < 400


class TestBrainClient(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.http = MagicMock()
        self.http.post = AsyncMock(return_value=_Response({"run_id": "run-1", "session_id": "ses-1"}))
        self.http.get = AsyncMock(return_value=_Response({"sessions": []}))
        self.http.aclose = AsyncMock()
        patcher = patch("jarvis_voice.brain_client.httpx.AsyncClient", return_value=self.http)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = MagicMock()
        self.state.notify_topic = AsyncMock()
        self.adapter = BrainClientAdapter(
            WatchedAgentConfig(id="brain", name="Brain", type="brain"), self.state
        )

    async def test_send_task_keeps_the_launch_folder_out_of_the_body(self) -> None:
        self.assertTrue(await self.adapter.send_task("Fix the login bug"))
        body = self.http.post.call_args.kwargs["json"]
        assert body["input"] == "Fix the login bug"
        assert "cwd" not in body
        assert "pinned" not in body
        assert self.adapter.busy  # the run queued behind the watcher

    async def test_pinned_session_sends_its_thread_and_folder(self) -> None:
        await self.adapter.pick_session(
            AgentSession(id="th-9", title="Login form", cwd="/Users/x/Projects/app", updated_at=1.0),
            connected=True,
        )
        self.assertTrue(await self.adapter.send_task("Continue"))
        body = self.http.post.call_args.kwargs["json"]
        assert body["pinned"] == {"runner": "t3", "session_id": "th-9"}
        assert body["cwd"] == "/Users/x/Projects/app"
        assert self.adapter.session_id == "th-9"

    async def test_answer_goes_back_as_free_text(self) -> None:
        self.adapter._run_id = "run-1"
        await self.adapter._handle_event(
            {"event": "question.request", "request_id": "q1", "description": "asks: Which database?"}
        )
        self.http.post = AsyncMock(return_value=_Response({"ok": True}))
        self.assertTrue(await self.adapter.answer("Postgres"))
        url = self.http.post.call_args.args[0]
        assert url == "/v1/runs/run-1/answer"
        assert self.http.post.call_args.kwargs["json"]["answer"] == "Postgres"

    async def test_a_finished_turn_is_retold_and_the_question_cleared(self) -> None:
        await self.adapter._handle_event(
            {"event": "question.request", "request_id": "q1", "description": "asks: Which database?"}
        )
        await self.adapter._handle_event({"event": "run.completed", "output": "Done."})
        assert self.adapter._pending is None
        self.state.notify_topic.assert_awaited()

    async def test_outbox_report_is_surfaced_and_acknowledged(self) -> None:
        self.http.post = AsyncMock(return_value=_Response({"ok": True}))
        await self.adapter._handle_outbox(
            {"id": "OB-1", "kind": "report", "summary_spoken": "Overnight task finished."}
        )
        self.state.notify_topic.assert_awaited()
        topic = self.state.notify_topic.await_args.args[0]
        assert topic.summary == "Overnight task finished."
        assert topic.is_report is True
        assert self.http.post.await_args.args[0] == "/v1/outbox/OB-1/spoken"

    async def test_outbox_approval_is_answered_through_the_outbox(self) -> None:
        self.http.post = AsyncMock(return_value=_Response({"ok": True}))
        await self.adapter._handle_outbox(
            {"id": "OB-2", "kind": "approval_request", "summary_spoken": "Approve the migration?"}
        )
        assert self.adapter._pending_outbox is not None
        self.http.post.reset_mock()
        self.http.post = AsyncMock(return_value=_Response({"ok": True}))
        assert await self.adapter.answer("yes, go ahead")
        assert self.http.post.await_args.args[0] == "/v1/outbox/OB-2/resolve"
        assert self.http.post.await_args.kwargs["json"] == {"approved": True}
        assert self.adapter._pending_outbox is None

    async def test_connected_session_surfaces_pending_prompt_on_start_and_answers_it(self) -> None:
        await self.adapter.pick_session(
            AgentSession(id="th-9", title="Login form", cwd="/Users/x/Projects/app", updated_at=1.0),
            connected=True,
        )
        self.http.post = AsyncMock(
            return_value=_Response(
                {
                    "pinned": {"runner": "t3", "session_id": "th-9"},
                    "pending": {"event": "question.request", "description": "asks: Which DB?"},
                }
            )
        )
        await self.adapter._attach_connected()
        self.state.notify_topic.assert_awaited()
        self.http.post = AsyncMock(return_value=_Response({"ok": True}))
        assert await self.adapter.answer("Postgres")
        assert self.http.post.await_args.args[0] == "/v1/connect"
        assert self.http.post.await_args.kwargs["json"] == {
            "runner": "t3",
            "session_id": "th-9",
            "answer": "Postgres",
        }


if __name__ == "__main__":
    unittest.main()
