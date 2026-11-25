from __future__ import annotations

import json
from pathlib import Path

from brain import memory
from brain.config import Settings
from brain.db import BrainDB, TraceRunRow
from brain.reconciler import (
    HARNESS_PREDICTION_RUNS,
    Reconciler,
    duplicate_harness_proposal,
    harness_spec_hash,
)
from brain.telemetry import TelemetryHub


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        home_dir=tmp_path / "home",
        projects_dir=tmp_path / "Projects",
        allowed_repos="jarvis",
        brain_api_key="secret-token",
        sandbox_enabled=False,
    )


def _proposal(db: BrainDB, surface: str = "brain_router", spec: str = "widen the router prompt"):
    return db.record_harness_proposal(
        surface=surface,
        spec_hash=harness_spec_hash(spec),
        title="Widen the router prompt",
        spec=spec,
        predicted_fix="fewer wrong projects",
        at_risk_regressions="slower routing",
        baseline_fail_rate=0.4,
        baseline_runs=20,
    )


def _run_row(trace_id: str, *, surface: str, created_at: str, failed: bool) -> TraceRunRow:
    return TraceRunRow(
        trace_id=trace_id,
        turn_id="t1",
        source="voice",
        surface=surface,
        runner="t3",
        repo="",
        task_id=None,
        attempt_id=None,
        outcome="errored" if failed else "ok",
        friction_kind="router_override" if failed else None,
        causal_sig="",
        latency_ms=900,
        ttfb_ms=None,
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        git_sha="master @ abc1234",
        prompt_sha="deadbeef",
        skills_used=(),
        user_input="do the thing",
        output_summary="done",
        trace_path="",
        superseded_by=None,
        reflected=0,
        created_at=created_at,
    )


def test_each_surface_has_its_own_proposal_slot(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    first = _proposal(db, "brain_router")
    second = _proposal(db, "voice_prompts", "tighten the retell prompt")
    assert first.id != second.id
    assert {p.surface for p in db.list_harness_proposals()} == {"brain_router", "voice_prompts"}
    queued = db.upsert_task(
        kind="problem",
        repo="jarvis",
        title="T",
        spec="S",
        verify_cmd="true",
        dedupe_key="harness:brain_router",
        ready=False,
    )
    assert queued.dedupe_key == "harness:brain_router"


def test_a_rejected_proposal_blocks_a_reworded_duplicate(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    original = _proposal(db)
    db.set_harness_outcome(proposal_id=original.id, outcome="rejected")
    # Same words, different punctuation: still the same idea.
    assert duplicate_harness_proposal(db, "brain_router", "Widen the router prompt!") is not None
    # A different mechanism is allowed through.
    assert duplicate_harness_proposal(db, "brain_router", "resolve the folder in code before the model call") is None
    # Another surface is a different component, whatever the words.
    assert duplicate_harness_proposal(db, "voice_prompts", "widen the router prompt") is None


async def test_ten_bad_runs_after_the_fix_falsify_its_prediction(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    proposal = _proposal(db)
    db.set_harness_outcome(proposal_id=proposal.id, outcome="pushed", applied_sha="abc1234")
    pushed = db.pushed_harness_proposals()[0]
    assert pushed.applied_at, "a pushed proposal records when its window starts"
    for index in range(HARNESS_PREDICTION_RUNS):
        db.upsert_trace_run(
            _run_row(
                f"{index}" * 32,
                surface="brain_router",
                created_at=pushed.applied_at or "2999-01-01T00:00:00Z",
                failed=True,
            )
        )
    rec = Reconciler(db, cfg, hub=TelemetryHub())
    await rec._check_harness_predictions()
    after = db.list_harness_proposals()[0]
    assert after.outcome == "falsified"
    outbox = [row for row in db.list_outbox() if row.kind == "approval_request"]
    assert outbox and "brain_router" in outbox[0].summary_spoken
    assert outbox[0].details_json and "post_fail_rate" in json.loads(outbox[0].details_json)


async def test_a_fix_that_held_is_verified(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    proposal = _proposal(db)
    db.set_harness_outcome(proposal_id=proposal.id, outcome="pushed", applied_sha="abc1234")
    applied = db.pushed_harness_proposals()[0].applied_at or ""
    for index in range(HARNESS_PREDICTION_RUNS):
        db.upsert_trace_run(
            _run_row(f"{index}" * 32, surface="brain_router", created_at=applied, failed=False)
        )
    await Reconciler(db, cfg, hub=TelemetryHub())._check_harness_predictions()
    assert db.list_harness_proposals()[0].outcome == "verified"


async def test_the_router_prompt_carries_the_standing_memory(tmp_path: Path) -> None:
    from brain.orchestrator import BrainOrchestrator

    cfg = _settings(tmp_path)
    memory.ensure_layout(cfg.home_dir)
    memory.index_path(cfg.home_dir).write_text(
        "# Brain memory index\n\n- pytest needs PYTHONPATH=src here.\n", encoding="utf-8"
    )
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    rec = Reconciler(db, cfg, hub=TelemetryHub())
    orch = BrainOrchestrator(cfg, db, rec, runners={})
    seen: list[str] = []

    async def fake_choose(prompt: str, user_input: str):
        seen.append(prompt)
        return {"name": "reply_direct", "arguments": {"spoken": "ok"}}

    orch._choose = fake_choose  # type: ignore[method-assign]
    await orch.decide("hello")
    await orch.close()
    assert "PYTHONPATH=src" in seen[0]


async def test_the_first_t3_message_carries_the_standing_memory(tmp_path: Path) -> None:
    from brain.runners import t3 as t3_module
    from brain.runners.base import RunRequest

    cfg = _settings(tmp_path)
    memory.ensure_layout(cfg.home_dir)
    memory.index_path(cfg.home_dir).write_text(
        "# Brain memory index\n\n- the dashboard builds with cargo.\n", encoding="utf-8"
    )
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    runner = t3_module.T3Runner(cfg, db)
    sent: list[dict] = []

    async def fake_call(method: str, params: dict):
        sent.append({"method": method, **params})
        return {"threadId": params.get("threadId")}

    async def fake_subscribe(state):
        return None

    runner._call = fake_call  # type: ignore[method-assign]
    runner._subscribe = fake_subscribe  # type: ignore[method-assign]
    original = t3_module.t3_defaults
    t3_module.t3_defaults = lambda: {"defaultModelSelection": {"provider": "x", "model": "y"}}  # type: ignore[assignment]
    try:
        state, created, error = await runner._launch(
            RunRequest(run_id="r1", prompt="fix the build"),
            tmp_path,
            {"id": "proj-1"},
            "jarvis",
        )
    finally:
        t3_module.t3_defaults = original
    assert error is None and created, state
    sent_text = sent[0]["initialMessage"]["text"]
    assert "the dashboard builds with cargo" in sent_text
    assert sent_text.index("Standing context") < sent_text.index("Sync with the master branch")


async def test_approving_falsified_harness_rollback_resets_repo_and_does_not_requeue_task(
    tmp_path: Path,
) -> None:
    import subprocess

    from aiohttp.test_utils import TestClient, TestServer

    from brain.server import BrainServer

    repo = tmp_path / "jarvis_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "master", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tester"], check=True)
    (repo / "app.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "app.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "v1"], check=True, capture_output=True)
    sha_v1 = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    (repo / "app.txt").write_text("v2-bad\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "app.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "v2"], check=True, capture_output=True)
    sha_v2 = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    # Reset back to v1 so `_apply_self_upgrade` applies v2 and records v1 as rollback_sha.
    subprocess.run(["git", "-C", str(repo), "reset", "--hard", sha_v1], check=True, capture_output=True)

    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=tmp_path / "Projects",
        self_repo_dir=repo,
        allowed_repos="jarvis",
        brain_api_key="secret-token",
        sandbox_enabled=False,
    )
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    task = db.upsert_task(
        kind="problem",
        repo="jarvis",
        title="Bad harness fix",
        spec="widen the router prompt",
        verify_cmd="true",
        dedupe_key="harness:brain_router",
        ready=True,
    )
    db.claim_next_task(task_id=task.id)
    db.finish_attempt(task_id=task.id, role="builder", summary="applied", outcome="pushed", git_sha=sha_v2)
    assert db.get_task(task.id).status == "done"

    db.record_harness_proposal(
        surface="brain_router",
        spec_hash=harness_spec_hash("widen the router prompt"),
        title="Bad harness fix",
        spec="widen the router prompt",
        predicted_fix="fewer wrong projects",
        at_risk_regressions="slower routing",
        baseline_fail_rate=0.2,
        baseline_runs=10,
        task_id=task.id,
    )
    rec = Reconciler(db, cfg, hub=TelemetryHub())
    assert await rec._apply_self_upgrade(repo, sha_v2, task.id)
    rec.restart_pending = False
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == sha_v2

    pushed = db.pushed_harness_proposals()[0]
    assert pushed.rollback_sha == sha_v1

    for index in range(HARNESS_PREDICTION_RUNS):
        db.upsert_trace_run(
            _run_row(
                f"{index}" * 32,
                surface="brain_router",
                created_at=pushed.applied_at or "2999-01-01T00:00:00Z",
                failed=True,
            )
        )
    await rec._check_harness_predictions()
    approvals = [row for row in db.list_outbox() if row.kind == "approval_request"]
    assert len(approvals) == 1
    assert approvals[0].task_id is None  # must NOT re-queue the broken task on approval

    server = BrainServer(db, rec, cfg)
    headers = {"Authorization": "Bearer secret-token"}
    async with TestClient(TestServer(server.app)) as client:
        resp = await client.post(
            f"/v1/outbox/{approvals[0].id}/resolve", json={"approved": True}, headers=headers
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["rolled_back"] is True

    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == sha_v1
    assert rec.restart_pending is True
    assert db.get_task(task.id).status == "done"
    assert db.list_harness_proposals()[0].outcome == "rolled_back"
    await rec.stop()
    db.close()
