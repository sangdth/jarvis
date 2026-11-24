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
from brain.voice_memory import curate_voice_memory


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


def test_curation_never_crosses_namespaces(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.rows = {
                "m1": {"id": "m1", "user_id": "sang", "memory": "the router dedupes by surface"},
                "m2": {"id": "m2", "user_id": "speaker:david", "memory": "David likes Postgres"},
            }
            self.deleted: list[str] = []

        def get(self, memory_id: str):
            return {"results": [self.rows[memory_id]]}

        def delete(self, memory_id: str) -> None:
            self.deleted.append(memory_id)

    client = FakeClient()
    promoted = curate_voice_memory(client, {"op": "promote", "user_id": "sang", "memory_id": "m1"})
    assert promoted["text"] == "the router dedupes by surface"
    with_as_owner = {"op": "promote", "user_id": "sang", "memory_id": "m2"}
    try:
        curate_voice_memory(client, with_as_owner)
    except ValueError as exc:
        assert "another namespace" in str(exc)
    else:
        raise AssertionError("a guest's memory was promoted into the owner's index")
    try:
        curate_voice_memory(client, {"op": "delete", "user_id": "sang", "memory_id": "m2"})
    except ValueError:
        pass
    else:
        raise AssertionError("a delete crossed namespaces")
    curate_voice_memory(client, {"op": "delete", "user_id": "speaker:david", "memory_id": "m2"})
    assert client.deleted == ["m2"]
    try:
        curate_voice_memory(None, {"op": "delete", "user_id": "sang", "memory_id": "m1"})
    except ValueError as exc:
        assert "not running" in str(exc)
    else:
        raise AssertionError("curation ran without the voice's client")
