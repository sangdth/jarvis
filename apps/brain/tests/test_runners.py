from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from brain.config import Settings
from brain.db import BrainDB
from brain.reconciler import Reconciler
from brain.runners.base import RunRequest, RunResult
from brain.runners.ephemeral_pi import EphemeralPiRunner
from brain.server import BrainServer
from jarvis_core.sessions import AgentSession


def _cfg(tmp_path: Path, **extra: Any) -> Settings:
    return Settings(
        home_dir=tmp_path / "home",
        projects_dir=tmp_path / "Projects",
        allowed_repos="jarvis",
        brain_api_key="secret",
        sandbox_enabled=False,
        openrouter_api_key="",  # the router must never run in tests
        **extra,
    )


def test_thread_binding_roundtrip(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    try:
        assert db.get_thread_binding("jarvis") is None
        db.set_thread_binding(
            repo="jarvis", runner="t3", thread_id="th-1", project_id="p-1",
            title="A thread", worktree_path="/tmp/wt", branch="brain/SG-1", last_sequence=42,
        )
        row = db.get_thread_binding("jarvis")
        assert row is not None
        assert (row.thread_id, row.runner, row.title, row.last_sequence) == ("th-1", "t3", "A thread", 42)
        db.set_thread_binding(repo="jarvis", runner="t3", thread_id="th-2")
        assert db.get_thread_binding("jarvis").thread_id == "th-2"  # type: ignore[union-attr]
        assert [r.repo for r in db.list_thread_bindings()] == ["jarvis"]
        db.clear_thread_binding("jarvis")
        assert db.get_thread_binding("jarvis") is None
    finally:
        db.close()


def test_old_attempts_table_is_rebuilt_with_the_new_roles(tmp_path: Path) -> None:
    """An existing state.db has the narrow role CHECK; opening it must widen it and keep its rows."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tasks (id TEXT PRIMARY KEY);
        INSERT INTO tasks (id) VALUES ('SG-1');
        CREATE TABLE attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('planner', 'builder', 'improver')),
            pi_session_id TEXT,
            session_jsonl_path TEXT,
            summary TEXT NOT NULL,
            git_branch TEXT,
            git_sha TEXT,
            cost_usd REAL NOT NULL DEFAULT 0.0,
            outcome TEXT NOT NULL CHECK (outcome IN ('pushed', 'decomposed', 'failed_verify',
                'deferred_approval', 'timed_out', 'errored', 'reaped')),
            verify_output TEXT,
            created_at TEXT NOT NULL
        );
        INSERT INTO attempts (task_id, role, summary, cost_usd, outcome, created_at)
        VALUES ('SG-1', 'builder', 'kept', 0.2, 'pushed', '2026-01-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    db = BrainDB(path, {"jarvis"})
    try:
        rows = db.list_attempts()
        assert [(r.role, r.summary, r.thread_id) for r in rows] == [("builder", "kept", None)]
        # The widened table accepts a runner role.
        db._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO attempts (task_id, role, summary, outcome, created_at) "
            "VALUES ('SG-1', 't3', 'new role', 'pushed', '2026-01-02T00:00:00Z')"
        )
        assert db.list_attempts()[-1].role == "t3"
    finally:
        db.close()


def test_task_runner_column_roundtrip(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    try:
        task = db.upsert_task(
            kind="problem", repo="jarvis", title="Fix it", spec="Broken",
            verify_cmd="true", runner="opencode", ready=True,
        )
        assert task.runner == "opencode"
        assert db.get_task(task.id).runner == "opencode"
        with pytest.raises(ValueError):
            db.upsert_task(kind="problem", repo="jarvis", title="x", spec="y", runner="nope")
    finally:
        db.close()


def _fake_pi(tmp_path: Path) -> Path:
    script = tmp_path / "fake_pi_answer.py"
    script.write_text(
        """#!/usr/bin/env python3
import json, sys
prompt = sys.argv[-1]
assert "one-shot" in prompt, prompt
print(json.dumps({"type": "session", "id": "sess-ephemeral"}), flush=True)
print(json.dumps({"type": "message_end",
                  "message": {"role": "assistant", "content": "Four, by most counts."},
                  "cost_usd": 0.03}), flush=True)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


async def test_ephemeral_runner_answers_spends_and_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, pi_bin=str(_fake_pi(tmp_path)))
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    work_dir = tmp_path / "ephemeral"
    monkeypatch.setattr(
        "brain.runners.ephemeral_pi.tempfile.mkdtemp",
        lambda **_: (work_dir.mkdir(), str(work_dir))[1],
    )
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    try:
        runner = EphemeralPiRunner(cfg, db)
        result = await runner.run(
            RunRequest(run_id="r1", prompt="Answer this one-shot: how many legs does a cat have?",
                       cwd=tmp_path),
            emit,
        )
        assert result.outcome == "pushed"
        assert result.summary == "Four, by most counts."
        assert abs(db.get_state().spent_today_usd - 0.03) < 1e-9
        assert not work_dir.exists(), "the one-shot temp dir must be destroyed"
        assert all("BRAIN_RPC_SOCK" not in e for e in events)
    finally:
        db.close()


async def test_ephemeral_chat_session_is_resumed_across_turns(tmp_path: Path) -> None:
    script = tmp_path / "fake_pi_chat.py"
    script.write_text(
        """#!/usr/bin/env python3
import json, pathlib, sys
args = sys.argv
session_dir = pathlib.Path(args[args.index("--session-dir") + 1])
session_id = args[args.index("--session-id") + 1] if "--session-id" in args else ""
(session_dir / "calls.txt").write_text(
    (session_dir / "calls.txt").read_text() + session_id + "\\n"
    if (session_dir / "calls.txt").exists()
    else session_id + "\\n"
)
print(json.dumps({"type": "session", "id": "sess-chat"}), flush=True)
print(json.dumps({"type": "message_end",
                  "message": {"role": "assistant", "content": "noted"},
                  "cost_usd": 0.01}), flush=True)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    cfg = _cfg(tmp_path, pi_bin=str(script))
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)

    async def emit(event: dict[str, Any]) -> None:
        return None

    try:
        runner = EphemeralPiRunner(cfg, db)
        first = await runner.run(
            RunRequest(run_id="c1", prompt="first", cwd=cfg.home_dir, session_key="chat-1"), emit
        )
        second = await runner.run(
            RunRequest(run_id="c2", prompt="second", cwd=cfg.home_dir, session_key="chat-1"), emit
        )
        assert first.outcome == second.outcome == "pushed"
        session_dir = cfg.chat_sessions_dir / "chat-1"
        assert session_dir.is_dir()
        assert (session_dir / "calls.txt").read_text().splitlines() == ["chat-1", "chat-1"]

        fresh = await runner.run(
            RunRequest(run_id="c3", prompt="no chat", cwd=cfg.home_dir), emit
        )
        assert fresh.outcome == "pushed"
    finally:
        db.close()


async def test_ephemeral_runner_stops_at_the_per_run_cost_cap(tmp_path: Path) -> None:
    script = tmp_path / "fake_pi_expensive_answer.py"
    script.write_text(
        """#!/usr/bin/env python3
import json, time
print(json.dumps({"type": "session", "id": "sess-cap"}), flush=True)
print(json.dumps({"type": "message_end",
                  "message": {"role": "assistant", "content": "expensive"},
                  "cost_usd": 5.0}), flush=True)
time.sleep(10)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    cfg = _cfg(tmp_path, pi_bin=str(script))
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)

    async def emit(event: dict[str, Any]) -> None:
        return None

    try:
        runner = EphemeralPiRunner(cfg, db)
        result = await runner.run(
            RunRequest(run_id="cap", prompt="answer", cwd=cfg.home_dir, max_cost_usd=1.0),
            emit,
        )
        assert result.outcome == "errored"
        assert "cost cap" in result.summary
        assert abs(db.get_state().spent_today_usd - 5.0) < 1e-9
    finally:
        db.close()


class FakeT3Runner:
    id = "t3"

    def __init__(self) -> None:
        self.answers: list[str] = []
        self.interrupted = False

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def list_sessions(self, cwd: str | None = None, limit: int = 15) -> list[AgentSession]:
        return [
            AgentSession(id="th-1", title="Login form", cwd=str(cwd or "/tmp/p"), updated_at=2.0, branch="brain/SG-1"),
            AgentSession(id="th-2", title="Docs", cwd=str(cwd or "/tmp/p"), updated_at=1.0),
        ]

    async def run(self, request: RunRequest, emit: Any) -> RunResult:
        await emit({
            "event": "question.request", "request_id": "q1",
            "description": "asks: Which database? Options: Postgres, SQLite.",
            "questions": [],
        })
        answer = await request.approval_cb("asks: Which database?", "", "th-1") if request.approval_cb else None
        if answer is None:
            return RunResult(outcome="deferred_approval", summary="Waiting on a question.",
                             session_id="th-1", question={"request_id": "q1"})
        self.answers.append(answer)
        return RunResult(outcome="pushed", summary=f"Chose {answer}.", session_id="th-1",
                         session_title="Login form")

    async def answer(self, session_id: str, answer: str) -> bool:
        self.answers.append(answer)
        return True

    async def interrupt(self, run_id: str) -> bool:
        self.interrupted = True
        return True


class DirectOrchestrator:
    """One runner, no model: proves the wire contract without an OpenRouter call."""

    def __init__(self, runner: FakeT3Runner) -> None:
        self.runner = runner
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def route(self, *, run_id: str, user_input: str, session_id: str, pinned: Any,
                    approval_cb: Any, emit: Any, screenshot: str | None = None,
                    session_mode: str | None = None, cwd: str | None = None,
                    chat_session_key: str | None = None, trace_id: str = "",
                    turn_id: str = "") -> RunResult:
        return await self.runner.run(
            RunRequest(run_id=run_id, prompt=user_input, approval_cb=approval_cb,
                       pinned_session_id=(pinned or {}).get("session_id")),
            emit,
        )


async def _events(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    async with client.get(f"/v1/runs/{run_id}/events") as sse:
        async for raw in sse.content:
            line = raw.decode("utf-8").strip()
            if line.startswith("data:"):
                event = json.loads(line.removeprefix("data:").strip())
                events.append(event)
                if event.get("event") in ("run.completed", "run.failed", "run.cancelled"):
                    break
    return events


async def test_question_is_answered_over_the_api_without_an_llm(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg)
    runner = FakeT3Runner()
    server = BrainServer(
        db, reconciler, cfg, runners={"t3": runner},  # type: ignore[dict-item]
        orchestrator=DirectOrchestrator(runner),
    )
    headers = {"Authorization": "Bearer secret"}
    try:
        async with TestClient(TestServer(server.app)) as client:
            resp = await client.post("/v1/runs", json={"input": "Add a login form"}, headers=headers)
            run_id = (await resp.json())["run_id"]
            events: list[dict[str, Any]] = []
            async with client.get(f"/v1/runs/{run_id}/events", headers=headers) as sse:
                async for raw in sse.content:
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line.removeprefix("data:").strip())
                    events.append(event)
                    if event.get("event") == "question.request":
                        answered = await client.post(
                            f"/v1/runs/{run_id}/answer",
                            json={"answer": "Postgres", "request_id": event["request_id"]},
                            headers=headers,
                        )
                        assert answered.status == 200
                    if event.get("event") in ("run.completed", "run.failed"):
                        break
            assert [e["event"] for e in events][0] == "question.request"
            assert any(e.get("event") == "approval.responded" for e in events)
            assert events[-1]["event"] == "run.completed"
            assert events[-1]["output"] == "Chose Postgres."
            assert runner.answers == ["Postgres"]
    finally:
        await reconciler.stop()
        await server.stop_background()
        db.close()


async def test_sessions_and_connect(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg)
    runner = FakeT3Runner()
    server = BrainServer(
        db, reconciler, cfg, runners={"t3": runner},  # type: ignore[dict-item]
        orchestrator=DirectOrchestrator(runner),
    )
    headers = {"Authorization": "Bearer secret"}
    try:
        async with TestClient(TestServer(server.app)) as client:
            listed = await client.get("/v1/sessions?runner=t3", headers=headers)
            assert listed.status == 200
            sessions = (await listed.json())["sessions"]
            assert [s["title"] for s in sessions] == ["Login form", "Docs"]

            pinned = await client.post(
                "/v1/connect", json={"runner": "t3", "session_id": "th-2"}, headers=headers
            )
            assert pinned.status == 200
            assert (await pinned.json())["pinned"] == {"runner": "t3", "session_id": "th-2"}
    finally:
        await reconciler.stop()
        await server.stop_background()
        db.close()


async def test_your_dashboard_routes_to_jarvis_board_while_generic_dashboard_stays_project(
    tmp_path: Path,
) -> None:
    from brain.orchestrator import BrainOrchestrator

    cfg = _cfg(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg)
    orch = BrainOrchestrator(cfg, db, reconciler, runners={}, last_project=lambda _: "opscom/platform")
    try:
        projects = ["jarvis", "opscom/dashboard", "opscom/platform"]

        bug = orch._heuristic("Fix the flickering bug in your dashboard", projects, "s1")
        assert bug["name"] == "queue_board_task"
        assert bug["arguments"]["repo"] == "jarvis"
        assert bug["arguments"]["kind"] == "problem"

        feat = orch._heuristic("Add a latency chart to Jarvis's dashboard", projects, "s1")
        assert feat["name"] == "queue_board_task"
        assert feat["arguments"]["repo"] == "jarvis"
        assert feat["arguments"]["kind"] == "goal"

        generic = orch._heuristic("Fix the login bug in the dashboard", ["jarvis", "opscom/platform"], "s1")
        assert generic["name"] == "delegate_project"
        assert generic["arguments"]["project"] == "opscom/platform"

        woke = False
        orig_wake = reconciler.wake

        def track_wake() -> None:
            nonlocal woke
            woke = True
            orig_wake()

        reconciler.wake = track_wake  # type: ignore[method-assign]
        res = await orch._queue_task(
            {"kind": "problem", "repo": "jarvis", "title": "Fix dashboard", "spec": "Fix apps/dashboard/"},
            "Fix your dashboard",
        )
        assert res.outcome == "pushed"
        assert woke is True
    finally:
        await orch.close()
        await reconciler.stop()
        db.close()


async def test_claude_and_opencode_runners_interrupt_and_cancel_kill_subprocesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import os

    from brain.runners.claude import ClaudeRunner
    from brain.runners.opencode import OpenCodeRunner

    pid_file = tmp_path / "child.pid"
    slow_cli = tmp_path / "fake_slow_cli.py"
    slow_cli.write_text(
        f"""#!/usr/bin/env python3
import os, pathlib, time
pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(30)
""",
        encoding="utf-8",
    )
    slow_cli.chmod(slow_cli.stat().st_mode | stat.S_IEXEC)
    cfg = _cfg(tmp_path)

    async def emit(_event: dict[str, Any]) -> None:
        return None

    def pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    for runner_cls, bin_name in ((ClaudeRunner, "claude"), (OpenCodeRunner, "opencode")):
        monkeypatch.setattr(
            f"brain.runners.{bin_name}.shutil.which",
            lambda name, target=bin_name: str(slow_cli) if name == target else None,
        )
        runner = runner_cls(cfg)

        # 1. `interrupt(run_id)` kills the active subprocess.
        pid_file.unlink(missing_ok=True)
        task = asyncio.create_task(
            runner.run(RunRequest(run_id="r-int", prompt="sleep", cwd=tmp_path), emit)
        )
        for _ in range(200):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert pid_file.exists()
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        assert await runner.interrupt("r-int") is True
        res = await asyncio.wait_for(task, timeout=5.0)
        assert res.outcome == "errored"
        assert not pid_alive(pid)

        # 2. Task cancellation kills the active subprocess in `finally:`.
        pid_file.unlink(missing_ok=True)
        task2 = asyncio.create_task(
            runner.run(RunRequest(run_id="r-cancel", prompt="sleep", cwd=tmp_path), emit)
        )
        for _ in range(200):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert pid_file.exists()
        pid2 = int(pid_file.read_text(encoding="utf-8").strip())
        task2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task2
        assert not pid_alive(pid2)
        assert "r-cancel" not in runner._active


async def test_queue_task_passes_run_key_and_adapted_approval_cb(tmp_path: Path) -> None:
    from brain.orchestrator import BrainOrchestrator

    cfg = _cfg(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg)
    orch = BrainOrchestrator(cfg, db, reconciler, runners={})
    captured: dict[str, Any] = {}

    async def fake_execute(task: Any, *, run_key: str | None = None, approval_cb: Any = None) -> Any:
        captured["run_key"] = run_key
        captured["approval_result"] = await approval_cb("Approve?", "", None) if approval_cb else None
        return task

    reconciler.execute_claimed_task = fake_execute  # type: ignore[method-assign]

    async def text_approval_cb(summary: str, details: str, session_id: str | None) -> str | None:
        return "yes, go ahead"

    try:
        res = await orch._queue_task(
            {
                "kind": "problem",
                "repo": "jarvis",
                "title": "Fix bug",
                "spec": "Fix it",
                "verify_cmd": "uv run pytest",
            },
            "Fix jarvis bug",
            run_id="run-abc123",
            approval_cb=text_approval_cb,
        )
        assert res.outcome == "pushed"
        assert captured["run_key"] == "run-abc123"
        assert captured["approval_result"] is True
    finally:
        await orch.close()
        await reconciler.stop()
        db.close()


async def test_deferred_t3_outbox_approval_forwards_answer_to_runner(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg)
    runner = FakeT3Runner()
    server = BrainServer(
        db, reconciler, cfg, runners={"t3": runner},  # type: ignore[dict-item]
        orchestrator=DirectOrchestrator(runner),
    )
    headers = {"Authorization": "Bearer secret"}
    try:
        async with TestClient(TestServer(server.app)) as client:
            ask = await client.post("/v1/ask", json={"input": "Add a login form"}, headers=headers)
            assert ask.status == 200
            queued = db.list_outbox()
            assert len(queued) == 1 and queued[0].kind == "approval_request"

            resolved = await client.post(
                f"/v1/outbox/{queued[0].id}/resolve",
                json={"approved": True, "answer": "Postgres"},
                headers=headers,
            )
            assert resolved.status == 200
            assert runner.answers == ["Postgres"]
    finally:
        await server.stop_background()
        await reconciler.stop()
        db.close()


async def test_claude_list_sessions_reads_jsonl_and_strips_gateway_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from brain.runners.claude import ClaudeRunner
    from brain.sandbox import strip_gateway_secrets

    claude_cfg = tmp_path / "claude"
    proj_dir = claude_cfg / "projects" / "my-app"
    proj_dir.mkdir(parents=True)
    app_cwd = (tmp_path / "Projects" / "my-app").resolve()
    app_cwd.mkdir(parents=True)
    other_cwd = (tmp_path / "Projects" / "other").resolve()
    other_cwd.mkdir(parents=True)

    (proj_dir / "11111111-1111-1111-1111-111111111111.jsonl").write_text(
        json.dumps({"type": "user", "cwd": str(app_cwd), "gitBranch": "feat/auth",
                    "message": {"content": "Fix JWT validation"}}) + "\n"
        + json.dumps({"customTitle": "JWT validation"}) + "\n",
        encoding="utf-8",
    )
    (proj_dir / "22222222-2222-2222-2222-222222222222.jsonl").write_text(
        json.dumps({"isSidechain": True, "type": "user", "cwd": str(app_cwd),
                    "message": {"content": "Subagent"}}) + "\n",
        encoding="utf-8",
    )
    (proj_dir / "33333333-3333-3333-3333-333333333333.jsonl").write_text(
        json.dumps({"type": "user", "cwd": str(other_cwd),
                    "message": {"content": "Other project prompt"}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_cfg))
    runner = ClaudeRunner(_cfg(tmp_path))
    all_sessions = await runner.list_sessions()
    assert len(all_sessions) == 2
    scoped = await runner.list_sessions(cwd=str(app_cwd))
    assert len(scoped) == 1
    assert scoped[0].title == "JWT validation"
    assert scoped[0].branch == "feat/auth"

    cleaned = strip_gateway_secrets(
        {"BRAIN_API_KEY": "owner-tok", "JARVIS_TOKEN": "owner-tok", "OPENROUTER_API_KEY": "keep-me"}
    )
    assert "BRAIN_API_KEY" not in cleaned and "JARVIS_TOKEN" not in cleaned
    assert cleaned["OPENROUTER_API_KEY"] == "keep-me"
    _ = os
