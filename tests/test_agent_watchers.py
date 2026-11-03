import asyncio
import unittest

import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ResultMessage

from jarvis.state import AgentTopic, JarvisState
from jarvis.agent_watchers import (
    AgentOrchestrator,
    ClaudeCodeAdapter,
    HermesAdapter,
    OpenCodeSSEAdapter,
    WatchedAgentConfig,
    form_answer,
    is_approval,
)


def make(adapter_cls, agent_type: str):
    cfg = WatchedAgentConfig(id="a1", name="Agent One", type=agent_type, cwd="/tmp")
    adapter = adapter_cls(cfg, state=MagicMock())
    adapter._surface = AsyncMock()
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
    def test_labels_with_punctuation_match(self) -> None:
        fields = [{"key": "q", "options": [{"value": "cpp", "label": "C++"}, {"value": "env", "label": ".env"}]}]
        self.assertEqual(form_answer(fields, "let's go with C++"), {"q": "cpp"})
        self.assertEqual(form_answer(fields, "put it in .env please"), {"q": "env"})

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
        self.adapter._surface.assert_awaited_with("Done.", is_report=True)
        await self.adapter._handle_message(MagicMock(spec=ResultMessage, result=None, is_error=True, subtype="error"))
        self.adapter._surface.assert_awaited_with("The turn failed (error).", is_report=True)

    async def test_bash_permission_is_asked_and_follows_the_answer(self) -> None:
        result = await self.ask_and_answer("Bash", {"command": "rm -rf build"}, "yes, go ahead")
        self.assertIsInstance(result, PermissionResultAllow)
        self.adapter._surface.assert_awaited_with(
            "needs approval: rm -rf build"
        )
        result = await self.ask_and_answer("Bash", {"command": "rm -rf build"}, "no, don't")
        self.assertIsInstance(result, PermissionResultDeny)

    async def test_ask_user_question_maps_the_answer_to_an_option(self) -> None:
        questions = [{"question": "Which database?", "header": "DB", "multiSelect": False,
                      "options": [{"label": "Postgres", "description": ""}, {"label": "SQLite", "description": ""}]}]
        result = await self.ask_and_answer("AskUserQuestion", {"questions": questions}, "let's use sqlite")
        self.assertEqual(result.updated_input["answers"], {"Which database?": "SQLite"})
        self.adapter._surface.assert_awaited_with(
            "asks: Which database? Options: Postgres, SQLite."
        )

    async def test_stop_while_waiting_denies_and_interrupts(self) -> None:
        self.adapter._running = True
        result = await self.ask_and_answer("Bash", {"command": "make deploy"}, None)
        self.assertIsInstance(result, PermissionResultDeny)
        self.assertTrue(result.interrupt)
        self.adapter._client.interrupt.assert_awaited_once()
        # The stopped turn ends with an error result; the user asked for it, so it is not read out.
        self.adapter._surface.reset_mock()
        await self.adapter._handle_message(MagicMock(spec=ResultMessage, result=None, is_error=True, subtype="x"))
        self.adapter._surface.assert_not_awaited()

    async def test_stop_while_idle_does_not_hide_the_next_result(self) -> None:
        await self.adapter.interrupt()
        await self.adapter._handle_message(MagicMock(spec=ResultMessage, result="Done.", is_error=False))
        self.adapter._surface.assert_awaited_once_with("Done.", is_report=True)

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
        await self.adapter._handle_event({"type": "session.text.ended", "data": {"sessionID": "ses_1", "text": "All tests pass."}})
        await self.adapter._handle_event({"type": "session.execution.succeeded", "data": {"sessionID": "ses_1"}})
        self.adapter._surface.assert_awaited_once_with("All tests pass.", is_report=True)

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
        self.adapter._surface.assert_awaited_once()
        self.assertEqual(self.adapter._pending_form["id"], "frm_1")

    async def test_stop_interrupts_the_session(self) -> None:
        self.adapter._session_id = "ses_1"
        with patch.object(self.adapter._http, "post", new=self.post):
            self.assertTrue(await self.adapter.interrupt())
        self.post.assert_awaited_once_with("/api/session/ses_1/interrupt")


class TestHermesAdapter(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.adapter = make(HermesAdapter, "hermes")

    async def asyncTearDown(self) -> None:
        await self.adapter._http.aclose()

    async def test_tasks_share_one_session(self) -> None:
        post = AsyncMock(return_value=MagicMock(json=MagicMock(side_effect=[{"run_id": "run_1"}, {"run_id": "run_2"}])))
        with patch.object(self.adapter._http, "post", new=post):
            self.assertTrue(await self.adapter.send_task("Check my calendar"))
            self.assertTrue(await self.adapter.send_task("Move it to 3pm"))
        self.assertEqual(post.call_args_list[0].kwargs["json"], {"input": "Check my calendar"})
        self.assertEqual(post.call_args_list[1].kwargs["json"], {"input": "Move it to 3pm", "session_id": "run_1"})
        self.assertEqual(self.adapter._runs.qsize(), 2)

    async def test_approval_is_read_out_and_answered_once(self) -> None:
        await self.adapter._handle_event(
            {"event": "approval.request", "run_id": "run_1", "request_id": "apr_1", "command": "rm -rf build"}
        )
        self.adapter._surface.assert_awaited_once_with("needs approval: rm -rf build")
        post = AsyncMock(return_value=MagicMock(is_success=True))
        with patch.object(self.adapter._http, "post", new=post):
            self.assertTrue(await self.adapter.answer("yes, go ahead"))
            self.assertFalse(await self.adapter.answer("yes"))  # nothing pending any more
        self.assertEqual(post.call_args.args[0], "/v1/runs/run_1/approval")
        self.assertEqual(post.call_args.kwargs["json"], {"choice": "once", "request_id": "apr_1"})

    async def test_stop_reaches_a_queued_run_after_the_one_before_ends(self) -> None:
        post = AsyncMock(return_value=MagicMock(json=MagicMock(side_effect=[{"run_id": "run_1"}, {"run_id": "run_2"}])))
        with patch.object(self.adapter._http, "post", new=post):
            await self.adapter.send_task("X")
            await self.adapter.send_task("Y")
        watched = []

        async def watch(run_id: str) -> bool:
            watched.append(run_id)
            if run_id == "run_2":
                await asyncio.sleep(3600)  # still running
            return True

        with patch.object(self.adapter, "_watch", new=watch):
            loop = asyncio.create_task(self.adapter._run_loop())
            await asyncio.sleep(0.05)
            self.assertEqual(watched, ["run_1", "run_2"])
            self.assertEqual(self.adapter._run_id, "run_2")  # run_1 ending did not wipe it
            self.assertTrue(self.adapter.busy)
            loop.cancel()

    async def test_failed_approval_reply_keeps_the_request(self) -> None:
        await self.adapter._handle_event({"event": "approval.request", "run_id": "run_1", "command": "rm x"})
        with patch.object(self.adapter._http, "post", new=AsyncMock(side_effect=httpx.ConnectError("down"))):
            self.assertFalse(await self.adapter.answer("yes"))
        self.assertIsNotNone(self.adapter._pending_approval)

    async def test_dropped_stream_falls_back_to_run_status(self) -> None:
        get = AsyncMock(return_value=MagicMock(json=MagicMock(return_value={"status": "completed", "output": "Done."})))
        with (
            patch.object(self.adapter._http, "stream", side_effect=httpx.ReadError("dropped")),
            patch.object(self.adapter._http, "get", new=get),
        ):
            self.assertTrue(await self.adapter._watch("run_1"))
        get.assert_awaited_once_with("/v1/runs/run_1")
        self.adapter._surface.assert_awaited_once_with("Done.", is_report=True)

        get.return_value = MagicMock(json=MagicMock(return_value={"status": "running"}))
        with (
            patch.object(self.adapter._http, "stream", side_effect=httpx.ReadError("dropped")),
            patch.object(self.adapter._http, "get", new=get),
        ):
            self.assertFalse(await self.adapter._watch("run_1"))  # still running: look again later

    async def test_run_end_events(self) -> None:
        await self.adapter._handle_event({"event": "run.cancelled", "run_id": "run_1"})
        self.adapter._surface.assert_not_awaited()  # the user stopped it
        await self.adapter._handle_event({"event": "run.completed", "run_id": "run_1", "output": "Booked."})
        self.adapter._surface.assert_awaited_with("Booked.", is_report=True)
        await self.adapter._handle_event({"event": "run.failed", "run_id": "run_1", "error": "no network"})
        self.adapter._surface.assert_awaited_with("The turn failed. no network", is_report=True)


def fake_adapter(agent_id: str, name: str = "Agent One", role: str = "") -> MagicMock:
    adapter = MagicMock()
    adapter.config.id = agent_id
    adapter.config.name = name
    adapter.config.role = role
    return adapter


class TestAgentRouting(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from jarvis.agent_watchers import AgentOrchestrator

        self.orch = AgentOrchestrator(state=MagicMock())

    def test_roster_lists_exact_ids_and_default(self) -> None:
        from jarvis.main import agent_roster

        self.assertIn("No coding agents", agent_roster(self.orch))
        self.orch.adapters["opencode"] = fake_adapter("opencode")
        with patch("jarvis.agent_watchers.settings.default_coding_agent", ""):
            roster = agent_roster(self.orch)
        self.assertIn("id `opencode` (DEFAULT for coding): Agent One", roster)
        self.assertIn("a coding task goes to `opencode`", roster)
        self.orch.adapters["hermes"] = fake_adapter("hermes", "Hermes", role="everything that is not coding")
        self.assertIn("id `hermes`: Hermes — everything that is not coding", agent_roster(self.orch))

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

        with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
            json.dump({"agents": [{"id": "claude", "name": "Claude", "type": "claude_code"}]}, f)
            f.flush()
            with patch("jarvis.agent_watchers.settings.default_coding_agent", "opencod"):
                with self.assertRaisesRegex(ValueError, "opencod"):
                    self.orch.load_from_file(f.name)

    def test_answer_and_stop_without_id_find_the_right_agent(self) -> None:
        state = JarvisState()
        orch = AgentOrchestrator(state=state)
        claude, hermes = fake_adapter("claude"), fake_adapter("hermes")
        claude.busy, hermes.busy = False, True
        orch.adapters.update(claude=claude, hermes=hermes)
        with patch("jarvis.agent_watchers.settings.default_coding_agent", "claude"):
            self.assertIs(orch.find_waiting(None), claude)  # nobody asked: the default
            state.active_agent_context["hermes"] = AgentTopic("hermes", "Hermes", "needs approval: rm x")
            self.assertIs(orch.find_waiting(None), hermes)  # Hermes asked, so "yes" is for Hermes
            self.assertIs(orch.find_busy(None), hermes)  # Hermes is the one working
            self.assertIs(orch.find_waiting("claude"), claude)  # a named agent always wins


if __name__ == "__main__":
    unittest.main()
