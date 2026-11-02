import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from jarvis.agent_watchers import (
    ACPStdioAdapter,
    ClaudeCodeStreamAdapter,
    OpenCodeSSEAdapter,
    WatchedAgentConfig,
    form_answer,
    is_approval,
)


def make(adapter_cls, agent_type: str):
    cfg = WatchedAgentConfig(id="a1", name="Agent One", type=agent_type, cwd="/tmp")
    adapter = adapter_cls(cfg, state=MagicMock(), executive_filter=MagicMock())
    adapter._handle_raw_event = AsyncMock()
    return adapter


class TestIsApproval(unittest.TestCase):
    def test_refusals_win_over_approval_words(self) -> None:
        for text in ("I'm not sure", "don't allow it", "no, stop", "wait, do not do it", "yes but not now"):
            self.assertFalse(is_approval(text), text)

    def test_clear_approvals(self) -> None:
        for text in ("yes", "go ahead", "Sure, approve it", "okay do it"):
            self.assertTrue(is_approval(text), text)

    def test_words_inside_other_words_do_not_count(self) -> None:
        self.assertFalse(is_approval("I saw that yesterday"))


QUESTION_FORM = {
    "id": "frm_1",
    "sessionID": "ses_1",
    "title": "Questions",
    "fields": [
        {
            "key": "q0",
            "title": "Next step",
            "description": "Which should come next: tests or docs?",
            "type": "string",
            "options": [{"value": "Tests", "label": "Tests"}, {"value": "Docs", "label": "Docs"}],
            "custom": True,
        }
    ],
}


class TestFormAnswer(unittest.TestCase):
    def test_named_option_wins(self) -> None:
        self.assertEqual(form_answer(QUESTION_FORM["fields"], "let's do the docs first"), {"q0": "Docs"})

    def test_free_text_when_no_option_named(self) -> None:
        self.assertEqual(form_answer(QUESTION_FORM["fields"], "skip both for now"), {"q0": "skip both for now"})

    def test_boolean_and_multiselect(self) -> None:
        fields = [
            {"key": "ok", "type": "boolean"},
            {"key": "pick", "type": "multiselect", "options": [{"value": "a", "label": "lint"}, {"value": "b", "label": "tests"}]},
        ]
        self.assertEqual(form_answer(fields, "yes, run lint and tests"), {"ok": True, "pick": ["a", "b"]})


class TestClaudeCodeAdapter(unittest.IsolatedAsyncioTestCase):
    async def test_result_and_question_events(self) -> None:
        adapter = make(ClaudeCodeStreamAdapter, "claude_code")

        await adapter._handle_event({"type": "result", "result": "Done with the refactor.", "is_error": False})
        adapter._handle_raw_event.assert_awaited_with("Done with the refactor.", force_decision=False)

        await adapter._handle_event({"type": "result", "result": "Build failed.", "is_error": True})
        adapter._handle_raw_event.assert_awaited_with("Build failed.", force_decision=True)

        question = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Use Postgres or SQLite?"}]}}
        await adapter._handle_event(question)
        adapter._handle_raw_event.assert_awaited_with("Use Postgres or SQLite?", force_decision=True)


class TestACPAdapter(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.adapter = make(ACPStdioAdapter, "acp_stdio")
        self.adapter._proc = MagicMock()
        self.adapter._write = AsyncMock()

    async def test_session_then_prompt_turn(self) -> None:
        self.adapter._requests = {2: "session/new"}
        await self.adapter._handle_message({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess_1"}})
        self.assertEqual(self.adapter._session_id, "sess_1")

        self.assertTrue(await self.adapter.send_instruction("Add a login page"))
        sent = self.adapter._write.await_args.args[0]
        self.assertEqual(sent["method"], "session/prompt")
        self.assertEqual(sent["params"]["sessionId"], "sess_1")

        chunk = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Login page added."}}
        await self.adapter._handle_message({"method": "session/update", "params": {"update": chunk}})
        await self.adapter._handle_message({"id": sent["id"], "result": {"stopReason": "end_turn"}})
        self.adapter._handle_raw_event.assert_awaited_once_with("Login page added.", force_decision=False)

    async def test_permission_rejected_picks_reject_option(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {
                "sessionId": "sess_1",
                "toolCall": {"toolCallId": "call_1", "title": "Delete build folder"},
                "options": [
                    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                ],
            },
        }
        await self.adapter._handle_message(request)
        self.adapter._handle_raw_event.assert_awaited_once()

        self.assertTrue(await self.adapter.send_instruction("I'm not sure, don't allow it"))
        reply = self.adapter._write.await_args.args[0]
        self.assertEqual(reply["id"], 7)
        self.assertEqual(reply["result"]["outcome"], {"outcome": "selected", "optionId": "reject-once"})

    async def test_unknown_agent_request_gets_error_reply(self) -> None:
        await self.adapter._handle_message({"jsonrpc": "2.0", "id": 9, "method": "fs/read_text_file", "params": {}})
        reply = self.adapter._write.await_args.args[0]
        self.assertEqual(reply["id"], 9)
        self.assertEqual(reply["error"]["code"], -32601)


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
        self.adapter._handle_raw_event.assert_awaited_once_with(
            "OpenCode needs approval: read /etc/hosts", force_decision=True
        )

        with patch.object(self.adapter._http, "post", new=self.post):
            self.assertTrue(await self.adapter.send_instruction("yes, go ahead"))
        self.post.assert_awaited_once_with("/api/session/ses_1/permission/per_1/reply", json={"decision": "once"})

    async def test_question_form_is_read_out_and_answered(self) -> None:
        # Shape captured from a live OpenCode v2.2.5 server: the form sits under data.form
        await self.adapter._handle_event({"type": "form.created", "data": {"form": QUESTION_FORM}})
        self.adapter._handle_raw_event.assert_awaited_once_with(
            "OpenCode asks: Which should come next: tests or docs? Options: Tests, Docs.", force_decision=True
        )
        with patch.object(self.adapter._http, "post", new=self.post):
            self.assertTrue(await self.adapter.send_instruction("Tests first"))
        self.post.assert_awaited_once_with("/api/session/ses_1/form/frm_1/reply", json={"answer": {"q0": "Tests"}})

    async def test_finished_turn_is_surfaced_and_next_message_is_a_prompt(self) -> None:
        await self.adapter._handle_event({"type": "session.text.ended", "data": {"sessionID": "ses_1", "text": "All tests pass."}})
        await self.adapter._handle_event({"type": "session.execution.succeeded", "data": {"sessionID": "ses_1"}})
        self.adapter._handle_raw_event.assert_awaited_once_with("All tests pass.", force_decision=False)

        with patch.object(self.adapter._http, "post", new=self.post):
            await self.adapter.send_instruction("Now add dark mode")
        self.post.assert_awaited_once_with("/api/session/ses_1/prompt", json={"text": "Now add dark mode"})


def fake_adapter(agent_id: str, name: str = "Agent One") -> MagicMock:
    adapter = MagicMock()
    adapter.config.id = agent_id
    adapter.config.name = name
    adapter.send_instruction = AsyncMock(return_value=True)
    return adapter


class TestAgentRouting(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from jarvis.agent_watchers import AgentOrchestrator

        self.orch = AgentOrchestrator(state=MagicMock(), executive_filter=MagicMock())

    def test_roster_lists_exact_ids_and_default(self) -> None:
        from jarvis.main import agent_roster

        self.assertIn("No coding agents", agent_roster(self.orch))
        self.orch.adapters["opencode-frontend"] = fake_adapter("opencode-frontend")
        with patch("jarvis.agent_watchers.settings.default_coding_agent", ""):
            roster = agent_roster(self.orch)
        self.assertIn("id `opencode-frontend` (DEFAULT): Agent One", roster)
        self.assertIn("send it to `opencode-frontend`", roster)

    async def test_default_agent_gets_unnamed_tasks_and_exact_ids_only(self) -> None:
        self.orch.adapters["claude-auth"] = fake_adapter("claude-auth")
        self.orch.adapters["opencode-frontend"] = fake_adapter("opencode-frontend")
        with patch("jarvis.agent_watchers.settings.default_coding_agent", "opencode-frontend"):
            self.assertEqual(await self.orch.route_user_decision(None, "build a login page"), "opencode-frontend")
            self.orch.adapters["opencode-frontend"].send_instruction.assert_awaited_once_with("build a login page")
            # A guessed id like "code" must not fall onto some agent
            self.assertIsNone(await self.orch.route_user_decision("code", "build it"))
        self.orch.adapters["claude-auth"].send_instruction.assert_not_awaited()

    def test_unknown_default_stops_startup(self) -> None:
        import json
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"agents": [{"id": "claude-auth", "name": "Claude", "type": "claude_code"}]}, f)
        with patch("jarvis.agent_watchers.settings.default_coding_agent", "opencod"):
            with self.assertRaisesRegex(ValueError, "opencod"):
                self.orch.load_from_file(f.name)


if __name__ == "__main__":
    unittest.main()
