import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from jarvis.agent_watchers import WatchedAgentConfig
from jarvis.t3_adapter import PROJECTS, PROJECTS_LABEL, T3Adapter, find_project, list_projects, t3_question_fields

RUN = "run:thread:t1:ordinal:1"


def make() -> T3Adapter:
    adapter = T3Adapter(WatchedAgentConfig(id="t3-code", name="T3 Code", type="t3", cwd="/tmp"), state=MagicMock())
    adapter._surface = AsyncMock()
    adapter._command = AsyncMock(return_value=True)
    adapter._thread_id = "t1"
    return adapter


def event(kind: str, payload: dict, run_id: str = RUN, sequence: int = 5) -> dict:
    return {"kind": "event", "sequence": sequence, "event": {"type": kind, "runId": run_id, "payload": payload}}


APPROVAL = {"type": "approval_request", "status": "waiting", "requestId": "rq1", "requestKind": "file-change",
            "prompt": "/tmp/x.txt", "reason": "Path is outside allowed working directories"}
QUESTION = {"type": "user_input_request", "status": "waiting", "requestId": "rq2", "questions": [
    {"id": "q1", "header": "Next", "question": "Tests or docs?",
     "options": [{"label": "Tests", "description": "t"}, {"label": "Docs", "description": "d", "value": "docs"}]}]}


class TestT3Adapter(unittest.IsolatedAsyncioTestCase):
    async def test_finished_run_is_retold_once(self) -> None:
        a = make()
        await a._on_stream_item(event("run.updated", {"id": RUN, "status": "running"}))
        self.assertTrue(a.busy)
        await a._on_stream_item(event("turn-item.updated", {"type": "assistant_message", "status": "completed",
                                                           "runId": RUN, "text": "All tests pass."}))
        await a._on_stream_item(event("run.updated", {"id": RUN, "status": "completed"}))
        a._surface.assert_awaited_once_with("All tests pass.", is_report=True)
        self.assertFalse(a.busy)

    async def test_stopped_run_is_not_news(self) -> None:
        a = make()
        await a._on_stream_item(event("run.updated", {"id": RUN, "status": "interrupted"}))
        a._surface.assert_not_awaited()

    async def test_approval_is_read_out_once_and_answered(self) -> None:
        a = make()
        await a._on_stream_item(event("turn-item.updated", APPROVAL))
        await a._on_stream_item({"kind": "snapshot", "snapshotSequence": 9, "projection": {"turnItems": [APPROVAL]}})
        a._surface.assert_awaited_once_with(
            "needs approval: /tmp/x.txt. Path is outside allowed working directories")
        self.assertTrue(await a.answer("no, don't"))
        a._command.assert_awaited_once_with({"type": "runtime-request.respond", "requestId": "rq1", "decision": "decline"})
        self.assertFalse(await a.answer("yes"))  # nothing pending any more

    async def test_question_answer_maps_to_option_value(self) -> None:
        a = make()
        await a._on_stream_item(event("turn-item.updated", QUESTION))
        a._surface.assert_awaited_once_with("asks: Tests or docs? Options: Tests, Docs.")
        await a.answer("docs please")
        a._command.assert_awaited_once_with({"type": "runtime-request.respond", "requestId": "rq2", "answers": {"q1": "docs"}})

    async def test_failed_reply_keeps_the_request(self) -> None:
        a = make()
        a._command.return_value = False
        await a._on_stream_item(event("turn-item.updated", APPROVAL))
        self.assertFalse(await a.answer("yes"))
        self.assertIsNotNone(a._pending)

    async def test_answered_in_t3_window_is_no_longer_pending(self) -> None:
        a = make()
        await a._on_stream_item(event("turn-item.updated", APPROVAL))
        await a._on_stream_item(event("runtime-request.updated", {"id": "rq1", "status": "resolved"}))
        self.assertIsNone(a._pending)
        self.assertFalse(await a.answer("yes"))

    async def test_stop_interrupts_running_runs_only(self) -> None:
        a = make()
        a._runs = {RUN: "running", "run:queued": "queued"}
        self.assertTrue(await a.interrupt())
        a._command.assert_awaited_once_with({"type": "run.interrupt", "runId": RUN, "reason": "Stopped by the user."})
        self.assertEqual(a._runs, {"run:queued": "queued"})

    async def test_stop_while_idle_does_nothing(self) -> None:
        a = make()
        self.assertFalse(await a.interrupt())
        a._command.assert_not_awaited()

    async def test_next_task_goes_to_the_same_thread(self) -> None:
        a = make()
        a._ws = MagicMock()
        self.assertTrue(await a.send_task("also update the README"))
        sent = a._command.await_args.args[0]
        self.assertEqual((sent["type"], sent["text"], sent["deliveryIntent"]), ("message.dispatch", "also update the README", "auto"))

    async def test_first_task_launches_a_thread_in_the_project(self) -> None:
        a = make()
        a._thread_id, a._ws = None, MagicMock()
        a._find_project = AsyncMock(return_value={"id": "p1", "workspaceRoot": "/tmp",
                                                  "defaultModelSelection": {"instanceId": "claudeAgent", "model": "m"}})
        a._call = AsyncMock(return_value={"threadId": "t9"})
        a._ws.send = AsyncMock()
        self.assertTrue(await a.send_task("fix the navbar"))
        method, payload = a._call.await_args.args
        self.assertEqual((method, payload["projectId"], payload["initialMessage"]["text"]),
                         ("orchestration.launchThread", "p1", "fix the navbar"))
        self.assertEqual(a._thread_id, "t9")
        subscribe = json.loads(a._ws.send.await_args.args[0])
        self.assertEqual((subscribe["tag"], subscribe["payload"]), ("orchestration.subscribeThread", {"threadId": "t9"}))

    async def test_project_without_a_model_uses_the_t3_default(self) -> None:
        a = make()
        a._thread_id, a._ws = None, MagicMock()
        a._ws.send = AsyncMock()
        a._find_project = AsyncMock(return_value={"id": "p1", "workspaceRoot": "/tmp", "defaultModelSelection": None})
        a._call = AsyncMock(return_value={"threadId": "t9"})
        defaults = {"defaultModelSelection": {"instanceId": "claudeAgent", "model": "g"}, "defaultRuntimeMode": "auto-accept-edits"}
        with patch("jarvis.t3_adapter.t3_defaults", return_value=defaults):
            self.assertTrue(await a.send_task("fix it"))
        payload = a._call.await_args.args[1]
        self.assertEqual((payload["modelSelection"]["model"], payload["runtimeMode"]), ("g", "auto-accept-edits"))

    async def test_new_project_is_created_and_synced_first(self) -> None:
        a = make()
        a._ws = MagicMock()
        a._ws.send = AsyncMock()
        a._find_project = AsyncMock(return_value=None)
        a._call = AsyncMock(side_effect=[{}, {"threadId": "t9"}])  # project.create, then launchThread
        defaults = {"defaultModelSelection": {"instanceId": "claudeAgent", "model": "g"}}
        with patch("jarvis.t3_adapter.list_projects", return_value=["huggerhustle/platform-v2"]), \
                patch("jarvis.t3_adapter.t3_defaults", return_value=defaults):
            self.assertTrue(await a.open_project("HuggerHustle/platform-v2", "fix the login"))
            created, launched = (c.args for c in a._call.await_args_list)
            root = str(PROJECTS / "huggerhustle/platform-v2")
            self.assertEqual((created[1]["type"], created[1]["workspaceRoot"]), ("project.create", root))
            self.assertEqual(launched[1]["projectId"], created[1]["projectId"])
            self.assertEqual(launched[1]["initialMessage"]["text"], "Sync with the master branch.")
            a._command.assert_not_awaited()  # the task is not sent to a new thread
            self.assertEqual(a._thread_id, "t9")
            self.assertTrue(await a.open_project(f"{PROJECTS_LABEL}/huggerhustle/platform-v2", "now the tests"))  # same project
        self.assertEqual(a._command.await_args.args[0]["text"], "now the tests")
        self.assertEqual(a._call.await_count, 2)  # no second thread

    async def test_opening_a_project_without_a_task_only_syncs(self) -> None:
        a = make()
        a._ws = MagicMock()
        a._ws.send = AsyncMock()
        a._find_project = AsyncMock(return_value=None)
        a._call = AsyncMock(side_effect=[{}, {"threadId": "t9"}])
        with patch("jarvis.t3_adapter.list_projects", return_value=["huggerhustle/platform-v2"]), \
                patch("jarvis.t3_adapter.t3_defaults", return_value={"defaultModelSelection": {"instanceId": "c", "model": "g"}}):
            self.assertTrue(await a.open_project("huggerhustle/platform-v2", ""))
            self.assertEqual(a._call.await_args.args[1]["initialMessage"]["text"], "Sync with the master branch.")
            a._command = AsyncMock()
            self.assertTrue(await a.open_project("huggerhustle/platform-v2", ""))  # already open
        a._command.assert_not_awaited()

    async def test_folder_outside_projects_is_refused(self) -> None:
        a = make()
        a._call = AsyncMock()
        with patch("jarvis.t3_adapter.list_projects", return_value=["HuggerHustle/platform-v2"]):
            self.assertFalse(await a.open_project("../../etc", "read it"))
        a._call.assert_not_awaited()

    async def test_resubscribe_resumes_after_last_sequence(self) -> None:
        a = make()
        a._ws = MagicMock()
        a._ws.send = AsyncMock()
        await a._on_stream_item(event("run.updated", {"id": RUN, "status": "running"}, sequence=42))
        await a._subscribe()
        self.assertEqual(json.loads(a._ws.send.await_args.args[0])["payload"], {"threadId": "t1", "afterSequence": 42})


class TestHelpers(unittest.TestCase):
    def test_deepest_live_project_holds_cwd(self) -> None:
        projects = [{"id": "a", "workspaceRoot": "/tmp"}, {"id": "b", "workspaceRoot": "/tmp/x"},
                    {"id": "c", "workspaceRoot": "/tmp/x/y", "archivedAt": "2026-01-01"}]
        self.assertEqual(find_project(projects, "/tmp/x/y/z")["id"], "b")
        self.assertIsNone(find_project(projects, "/var"))

    def test_projects_are_repos_one_or_two_folders_deep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for repo in ("solo/.git", "Org/app/.git", "Org/notes", ".hidden/x/.git", "Org/app/deep/.git"):
                (root / repo).mkdir(parents=True)
            self.assertEqual(list_projects(root), ["Org/app", "solo"])

    def test_question_fields_use_value_or_label(self) -> None:
        fields = t3_question_fields(QUESTION["questions"])
        self.assertEqual([o["value"] for o in fields[0]["options"]], ["Tests", "docs"])


if __name__ == "__main__":
    unittest.main()
