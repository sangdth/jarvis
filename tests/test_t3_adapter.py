import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from jarvis.agent_adapter import WatchedAgentConfig
from jarvis.t3_adapter import (
    PROJECTS,
    PROJECTS_LABEL,
    T3Adapter,
    current_thread,
    find_project,
    list_projects,
    recent_threads,
    t3_question_fields,
)

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
        self.assertEqual(
            (sent["type"], sent["text"], sent["deliveryIntent"]), ("message.dispatch", "also update the README", "auto")
        )

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
        defaults = {
            "defaultModelSelection": {"instanceId": "claudeAgent", "model": "g"}, "defaultRuntimeMode": "auto-accept-edits"
        }
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

    async def test_connected_thread_never_starts_a_new_one(self) -> None:
        a = make()
        a.attach("t9", "/Users/x/.t3/worktrees/platform-v2/t3-abc", connected=True)  # a worktree, not ~/Projects/...
        a._ws = MagicMock()
        a._call = AsyncMock()
        with patch("jarvis.t3_adapter.list_projects", return_value=["huggerhustle/platform-v2", "jarvis"]):
            self.assertTrue(await a.open_project("huggerhustle/platform-v2", "ok go ahead"))
            self.assertTrue(await a.open_project("jarvis", "and the tests"))  # another project: still this thread
            self.assertEqual(await a.open_project("jarvis", ""), "nothing_to_send")
            self.assertEqual(await a.open_project("misheard", ""), "nothing_to_send")
        self.assertFalse(await a._launch("/tmp", "x", create=True))
        a._call.assert_not_awaited()  # no project.create, no launchThread
        self.assertEqual([c.args[0]["text"] for c in a._command.await_args_list], ["ok go ahead", "and the tests"])
        self.assertEqual(a._thread_id, "t9")

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

    async def test_attached_thread_reads_out_its_waiting_question_but_sends_nothing(self) -> None:
        a = make()
        a.attach("t9", "/tmp/repo")
        a._ws = MagicMock()
        a._ws.send = AsyncMock()
        await a._subscribe()
        self.assertEqual(json.loads(a._ws.send.await_args.args[0])["payload"], {"threadId": "t9"})
        await a._on_stream_item({"kind": "snapshot", "snapshotSequence": 3, "projection": {"turnItems": [APPROVAL]}})
        a._surface.assert_awaited_once()
        self.assertTrue(a._surface.await_args.args[0].startswith("needs approval:"))
        a._command.assert_not_awaited()

    async def test_snapshot_ignores_completed_messages_from_past_runs(self) -> None:
        a = make()
        past = {"type": "assistant_message", "status": "completed", "runId": "run:old", "text": "Old"}
        active = {"type": "assistant_message", "status": "completed", "runId": RUN, "text": "Current"}
        await a._on_stream_item(
            {
                "kind": "snapshot",
                "snapshotSequence": 5,
                "projection": {"runs": [{"id": RUN, "status": "running"}], "turnItems": [past, active]},
            }
        )
        self.assertEqual(a._last_text, {RUN: "Current"})

    async def test_shell_snapshot_reuses_the_live_websocket(self) -> None:
        a = make()
        sent: list[dict] = []

        async def send(raw: str) -> None:
            msg = json.loads(raw)
            sent.append(msg)
            if msg.get("tag") == "orchestration.subscribeShell":
                await a._on_message(
                    a._ws,
                    {
                        "_tag": "Chunk",
                        "requestId": msg["id"],
                        "values": [{"kind": "snapshot", "snapshot": {"projects": [{"id": "p1"}]}}],
                    },
                )

        a._ws = MagicMock(send=AsyncMock(side_effect=send))
        self.assertEqual(await a.shell_snapshot(), {"projects": [{"id": "p1"}]})
        self.assertEqual([m["_tag"] for m in sent], ["Request", "Ack", "Interrupt"])


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
            os.utime(root / "solo/.git", (1, 1))
            self.assertEqual(list_projects(root), ["Org/app", "solo"])
            (root / "solo/.git/index").touch()  # git wrote the index just now: solo is the newest
            self.assertEqual(list_projects(root), ["solo", "Org/app"])
            with patch("jarvis.t3_adapter._last_touched", side_effect=AssertionError("should not stat")):
                self.assertEqual(list_projects(root, sort=False), ["Org/app", "solo"])

    def test_recent_threads_are_the_users_own_newest_first(self) -> None:
        snapshot = {
            "projects": [{"id": "p1", "title": "jarvis", "workspaceRoot": "/r/jarvis"}],
            "threads": [
                {"id": "old", "projectId": "p1", "createdBy": "user", "title": "Old", "updatedAt": "2026-01-01T00:00:00Z"},
                {"id": "new", "projectId": "p1", "createdBy": "user", "title": "New", "status": "running",
                 "updatedAt": "2026-02-01T00:00:00Z", "worktreePath": "/wt/new"},
                {"id": "child", "projectId": "p1", "createdBy": "agent", "updatedAt": "2026-03-01T00:00:00Z"},
                {"id": "import", "projectId": "p1", "createdBy": "system", "updatedAt": "2026-03-01T00:00:00Z"},
                {"id": "gone", "projectId": "p1", "createdBy": "user", "archivedAt": "x", "updatedAt": "2026-03-01"},
                {"id": "orphan", "projectId": "p2", "createdBy": "user", "updatedAt": "2026-03-01T00:00:00Z"},
            ],
        }
        threads = recent_threads(snapshot)
        self.assertEqual([t["id"] for t in threads], ["new", "old"])
        self.assertEqual((threads[0]["root"], threads[1]["root"]), ("/wt/new", "/r/jarvis"))
        self.assertEqual((threads[0]["project"], threads[1]["status"]), ("jarvis", "idle"))
        self.assertEqual(len(recent_threads(snapshot, limit=1)), 1)

    def test_project_script_finds_the_thread_it_runs_in(self) -> None:
        def thread(id_: str, visited: str, worktree: str | None = None) -> dict:
            return {"id": id_, "projectId": "p1", "createdBy": "user", "updatedAt": "2026-01-01T00:00:00Z",
                    "lastVisitedAt": visited, "worktreePath": worktree}
        snapshot = {
            "projects": [{"id": "p1", "workspaceRoot": "/r/jarvis"}],
            "threads": [thread("wt", "2026-03-01", "/wt/a"), thread("root-old", "2026-01-01"),
                        thread("root-seen", "2026-02-01"), thread("wt-b", "2026-04-01", "/wt/b")],
        }
        self.assertEqual(current_thread(snapshot, "/r/jarvis", "/wt/a")["id"], "wt")  # a worktree is exact
        self.assertEqual(current_thread(snapshot, "/r/jarvis", None)["id"], "root-seen")  # root: viewed last
        self.assertIsNone(current_thread(snapshot, "/r/jarvis", "/wt/gone"))
        self.assertIsNone(current_thread(snapshot, "/r/other", None))

    def test_question_fields_use_value_or_label(self) -> None:
        fields = t3_question_fields(QUESTION["questions"])
        self.assertEqual([o["value"] for o in fields[0]["options"]], ["Tests", "docs"])


if __name__ == "__main__":
    unittest.main()
