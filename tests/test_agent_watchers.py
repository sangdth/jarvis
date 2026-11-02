import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ResultMessage

from jarvis.agent_watchers import (
    ClaudeCodeAdapter,
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


def context(**kw):
    ctx = MagicMock(title=None, description=None)
    ctx.configure_mock(**kw)
    return ctx


class TestClaudeCodeAdapter(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.adapter = make(ClaudeCodeAdapter, "claude_code")
        self.adapter._client = MagicMock(query=AsyncMock(), interrupt=AsyncMock())

    async def ask_and_answer(self, tool: str, tool_input: dict, answer: str | None, ctx=None):
        """Start a tool permission check, answer it like the user would, and return the SDK result."""
        pending = asyncio.create_task(self.adapter._can_use_tool(tool, tool_input, ctx or context()))
        while self.adapter._question is None:
            await asyncio.sleep(0)
        if answer is None:
            await self.adapter.interrupt()
        else:
            self.assertTrue(await self.adapter.answer(answer))
        return await pending

    async def test_result_is_surfaced_and_errors_are_forced(self) -> None:
        await self.adapter._handle_message(MagicMock(spec=ResultMessage, result="Done.", is_error=False))
        self.adapter._handle_raw_event.assert_awaited_with("Done.", force_decision=False)
        await self.adapter._handle_message(MagicMock(spec=ResultMessage, result=None, is_error=True, subtype="error"))
        self.adapter._handle_raw_event.assert_awaited_with("The turn failed (error).", force_decision=True)

    async def test_bash_permission_is_asked_and_follows_the_answer(self) -> None:
        result = await self.ask_and_answer("Bash", {"command": "rm -rf build"}, "yes, go ahead")
        self.assertIsInstance(result, PermissionResultAllow)
        self.adapter._handle_raw_event.assert_awaited_with(
            "Claude Code needs approval: rm -rf build", force_decision=True
        )
        result = await self.ask_and_answer("Bash", {"command": "rm -rf build"}, "no, don't")
        self.assertIsInstance(result, PermissionResultDeny)

    async def test_ask_user_question_maps_the_answer_to_an_option(self) -> None:
        questions = [{"question": "Which database?", "header": "DB", "multiSelect": False,
                      "options": [{"label": "Postgres", "description": ""}, {"label": "SQLite", "description": ""}]}]
        result = await self.ask_and_answer("AskUserQuestion", {"questions": questions}, "let's use sqlite")
        self.assertEqual(result.updated_input["answers"], {"Which database?": "SQLite"})
        self.adapter._handle_raw_event.assert_awaited_with(
            "Claude Code asks: Which database? Options: Postgres, SQLite.", force_decision=True
        )

    async def test_stop_while_waiting_denies_and_interrupts(self) -> None:
        self.adapter._running = True
        result = await self.ask_and_answer("Bash", {"command": "make deploy"}, None)
        self.assertIsInstance(result, PermissionResultDeny)
        self.assertTrue(result.interrupt)
        self.adapter._client.interrupt.assert_awaited_once()
        # The stopped turn ends with an error result; the user asked for it, so it is not read out.
        self.adapter._handle_raw_event.reset_mock()
        await self.adapter._handle_message(MagicMock(spec=ResultMessage, result=None, is_error=True, subtype="x"))
        self.adapter._handle_raw_event.assert_not_awaited()

    async def test_stop_while_idle_does_not_hide_the_next_result(self) -> None:
        await self.adapter.interrupt()
        await self.adapter._handle_message(MagicMock(spec=ResultMessage, result="Done.", is_error=False))
        self.adapter._handle_raw_event.assert_awaited_once_with("Done.", force_decision=False)

    async def test_answer_without_question_and_new_task(self) -> None:
        self.assertFalse(await self.adapter.answer("yes"))
        self.assertTrue(await self.adapter.send_task("add a login page"))
        self.adapter._client.query.assert_awaited_once_with("add a login page")


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
            self.assertTrue(await self.adapter.answer("yes, go ahead"))
        self.post.assert_awaited_once_with("/api/session/ses_1/permission/per_1/reply", json={"decision": "once"})

    async def test_question_form_is_read_out_and_answered(self) -> None:
        # Shape captured from a live OpenCode v2.2.5 server: the form sits under data.form
        await self.adapter._handle_event({"type": "form.created", "data": {"form": QUESTION_FORM}})
        self.adapter._handle_raw_event.assert_awaited_once_with(
            "OpenCode asks: Which should come next: tests or docs? Options: Tests, Docs.", force_decision=True
        )
        with patch.object(self.adapter._http, "post", new=self.post):
            self.assertTrue(await self.adapter.answer("Tests first"))
        self.post.assert_awaited_once_with("/api/session/ses_1/form/frm_1/reply", json={"answer": {"q0": "Tests"}})

    async def test_finished_turn_is_surfaced_and_next_message_is_a_prompt(self) -> None:
        await self.adapter._handle_event({"type": "session.text.ended", "data": {"sessionID": "ses_1", "text": "All tests pass."}})
        await self.adapter._handle_event({"type": "session.execution.succeeded", "data": {"sessionID": "ses_1"}})
        self.adapter._handle_raw_event.assert_awaited_once_with("All tests pass.", force_decision=False)

        with patch.object(self.adapter._http, "post", new=self.post):
            self.assertFalse(await self.adapter.answer("Now add dark mode"))
            await self.adapter.send_task("Now add dark mode")
        self.post.assert_awaited_once_with("/api/session/ses_1/prompt", json={"text": "Now add dark mode"})

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
        self.adapter._handle_raw_event.assert_awaited_once()
        self.assertEqual(self.adapter._pending_form["id"], "frm_1")

    async def test_stop_interrupts_the_session(self) -> None:
        self.adapter._session_id = "ses_1"
        with patch.object(self.adapter._http, "post", new=self.post):
            self.assertTrue(await self.adapter.interrupt())
        self.post.assert_awaited_once_with("/api/session/ses_1/interrupt")


def fake_adapter(agent_id: str, name: str = "Agent One") -> MagicMock:
    adapter = MagicMock()
    adapter.config.id = agent_id
    adapter.config.name = name
    return adapter


class TestAgentRouting(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from jarvis.agent_watchers import AgentOrchestrator

        self.orch = AgentOrchestrator(state=MagicMock(), executive_filter=MagicMock())

    def test_roster_lists_exact_ids_and_default(self) -> None:
        from jarvis.main import agent_roster

        self.assertIn("No coding agents", agent_roster(self.orch))
        self.orch.adapters["opencode"] = fake_adapter("opencode")
        with patch("jarvis.agent_watchers.settings.default_coding_agent", ""):
            roster = agent_roster(self.orch)
        self.assertIn("id `opencode` (DEFAULT): Agent One", roster)
        self.assertIn("send it to `opencode`", roster)

    def test_default_agent_gets_unnamed_tasks_and_exact_ids_only(self) -> None:
        self.orch.adapters["claude"] = fake_adapter("claude")
        self.orch.adapters["opencode"] = fake_adapter("opencode")
        with patch("jarvis.agent_watchers.settings.default_coding_agent", "opencode"):
            self.assertIs(self.orch.find(None), self.orch.adapters["opencode"])
            self.assertIs(self.orch.find("claude"), self.orch.adapters["claude"])
            # A guessed id like "code" must not fall onto some agent
            self.assertIsNone(self.orch.find("code"))

    def test_unknown_default_stops_startup(self) -> None:
        import json
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"agents": [{"id": "claude", "name": "Claude", "type": "claude_code"}]}, f)
        with patch("jarvis.agent_watchers.settings.default_coding_agent", "opencod"):
            with self.assertRaisesRegex(ValueError, "opencod"):
                self.orch.load_from_file(f.name)


if __name__ == "__main__":
    unittest.main()
