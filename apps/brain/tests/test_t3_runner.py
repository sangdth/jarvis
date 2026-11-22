"""The router and T3 thread resolution, without a live T3 or OpenRouter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from brain.config import Settings
from brain.db import BrainDB
from brain.orchestrator import BrainOrchestrator
from brain.reconciler import Reconciler
from brain.runners.base import RunRequest, RunResult
from brain.runners.t3 import T3Runner, find_project, recent_threads


def _cfg(tmp_path: Path) -> Settings:
    projects = tmp_path / "Projects"
    (projects / "app" / ".git").mkdir(parents=True)
    return Settings(
        home_dir=tmp_path / "home", projects_dir=projects, allowed_repos="jarvis",
        brain_api_key="secret", openrouter_api_key="", sandbox_enabled=False,
    )


def _snapshot(root: Path) -> dict[str, Any]:
    return {
        "projects": [{"id": "p1", "workspaceRoot": str(root), "title": "App"}],
        "threads": [
            {"id": "th-1", "projectId": "p1", "title": "Newest", "createdBy": "user",
             "status": "idle", "updatedAt": "2026-11-22T10:00:00Z", "branch": "master",
             "worktreePath": None},
            {"id": "th-2", "projectId": "p1", "title": "Older", "createdBy": "user",
             "status": "running", "updatedAt": "2026-11-21T10:00:00Z", "branch": "master",
             "worktreePath": None},
            {"id": "child", "projectId": "p1", "title": "Agent child", "createdBy": "agent",
             "status": "idle", "updatedAt": "2026-11-22T11:00:00Z"},
        ],
    }


def test_recent_threads_keep_the_users_own_newest_first() -> None:
    titles = [t["title"] for t in recent_threads(_snapshot(Path("/Users/x/Projects/app")))]
    assert titles == ["Newest", "Older"]


def test_find_project_picks_the_deepest_folder() -> None:
    projects = [
        {"id": "outer", "workspaceRoot": "/Users/x/Projects"},
        {"id": "inner", "workspaceRoot": "/Users/x/Projects/app"},
    ]
    found = find_project(projects, "/Users/x/Projects/app/sub")
    assert found is not None and found["id"] == "inner"


class _FakeT3(T3Runner):
    def __init__(self, cfg: Settings, db: BrainDB) -> None:
        super().__init__(cfg, db)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.subscribed: list[str] = []

    async def shell_snapshot(self) -> dict[str, Any]:
        return _snapshot(self.cfg.projects_dir / "app")

    async def _subscribe(self, state: Any) -> None:
        self.subscribed.append(state.thread_id)

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        self.calls.append((method, payload))
        return {"threadId": payload.get("threadId")}


async def test_t3_resolution_idle_bound_and_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    monkeypatch.setattr(
        "brain.runners.t3.t3_defaults",
        lambda: {"defaultModelSelection": {"providerInstanceId": "p", "model": "m"},
                 "defaultRuntimeMode": "auto"},
    )
    try:
        runner = _FakeT3(cfg, db)
        cwd = cfg.projects_dir / "app"

        # No binding: the newest idle thread, never the running one and never an agent child.
        state, launched, error = await runner._resolve_thread(
            RunRequest(run_id="r1", prompt="Continue"), cwd, str(cwd)
        )
        assert error is None and state is not None and launched
        assert state.thread_id == "th-1"
        assert runner.subscribed == ["th-1"]

        # A binding wins over the newest idle thread.
        db.set_thread_binding(repo=str(cwd), runner="t3", thread_id="th-1")
        state2, launched2, _ = await runner._resolve_thread(
            RunRequest(run_id="r2", prompt="More"), cwd, str(cwd)
        )
        assert state2 is not None and state2.thread_id == "th-1" and not launched2

        # "new session" launches a thread with the user's T3 defaults, and the brain remembers it.
        state3, launched3, error3 = await runner._resolve_thread(
            RunRequest(run_id="r3", prompt="New thing", session="new"), cwd, str(cwd)
        )
        assert error3 is None and state3 is not None and launched3
        launch = next(payload for method, payload in runner.calls if method == "orchestration.launchThread")
        assert launch["modelSelection"] == {"providerInstanceId": "p", "model": "m"}
        assert launch["runtimeMode"] == "auto"
        assert launch["workspaceStrategy"] == {"type": "root"}
        assert launch["initialMessage"]["text"].startswith("Sync with the master branch first.")
        assert db.get_thread_binding(str(cwd)) is not None
    finally:
        db.close()


async def test_router_dispatches_a_question_to_the_one_shot_runner(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg)
    seen: list[RunRequest] = []

    class FakeEphemeral:
        id = "ephemeral"

        async def run(self, request: RunRequest, emit: Any) -> RunResult:
            seen.append(request)
            return RunResult(outcome="pushed", summary="Four.")

    async def no_router(prompt: str, user_input: str) -> dict[str, Any]:
        return {"name": "ask_ephemeral_pi", "arguments": {"question": "How many legs?"}}

    async def emit(event: dict[str, Any]) -> None:
        return None

    try:
        orchestrator = BrainOrchestrator(cfg, db, reconciler, runners={"ephemeral": FakeEphemeral()})
        orchestrator._choose = no_router  # type: ignore[method-assign]
        result = await asyncio.wait_for(
            orchestrator.route(
                run_id="r1", user_input="How many legs does a cat have?", session_id="s1",
                pinned=None, approval_cb=None, emit=emit,
            ),
            timeout=5,
        )
        assert result.summary == "Four."
        assert seen and "one or two short sentences" in seen[0].prompt
        await orchestrator.close()
    finally:
        await reconciler.stop()
        db.close()
