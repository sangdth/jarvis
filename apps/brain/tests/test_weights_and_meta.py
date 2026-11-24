from __future__ import annotations

import json
from pathlib import Path

from brain.config import Settings
from brain.db import BrainDB, TraceRunRow
from brain.reconciler import PROBATION_MIN_TASKS, PROBATION_MIN_TURNS, Reconciler
from brain.telemetry import TelemetryHub
from brain.weights import current, laya_intent_ready, promote, rollback, status


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        home_dir=tmp_path / "home",
        projects_dir=tmp_path / "Projects",
        allowed_repos="jarvis",
        sandbox_enabled=False,
    )


def test_promote_then_rollback_keeps_the_old_checkpoint(tmp_path: Path) -> None:
    home = tmp_path / "home"
    first = home / "models" / "laya-intent" / "v1-a"
    second = home / "models" / "laya-intent" / "v2-b"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    promote(home, "laya-intent", first)
    assert current(home, "laya-intent") == first
    promote(home, "laya-intent", second)
    assert current(home, "laya-intent") == second
    rows = {row["target"]: row for row in status(home)}
    assert rows["laya-intent"]["current"] == second.name
    assert rows["laya-intent"]["previous"] == first.name
    restored = rollback(home, "laya-intent")
    assert restored == first
    assert current(home, "laya-intent") == first


def test_rollback_without_a_previous_checkpoint_is_a_noop(tmp_path: Path) -> None:
    assert rollback(tmp_path / "home", "router-distill") is None


def test_an_unknown_weight_target_is_refused(tmp_path: Path) -> None:
    try:
        promote(tmp_path / "home", "my-model", tmp_path)
    except ValueError as exc:
        assert "unknown weight target" in str(exc)
    else:
        raise AssertionError("an unknown target was promoted")


def test_laya_needs_twenty_five_new_corrections(tmp_path: Path) -> None:
    home = tmp_path / "home"
    feedback = home / "feedback" / "intent.jsonl"
    feedback.parent.mkdir(parents=True)
    feedback.write_text("\n".join(json.dumps({"id": f"d{i}"}) for i in range(10)) + "\n", encoding="utf-8")
    ready, why = laya_intent_ready(home)
    assert not ready and "10 new corrections" in why
    feedback.write_text(
        "\n".join(json.dumps({"id": f"d{i}"}) for i in range(30)) + "\n", encoding="utf-8"
    )
    assert laya_intent_ready(home)[0]


def _seed_meta(db: BrainDB, *, tasks: int, failures: int, turns: int, bad_turns: int) -> None:
    for index in range(tasks):
        task = db.upsert_task(
            kind="problem", repo="jarvis", title=f"T{index}", spec="S", verify_cmd="true", ready=True
        )
        db.claim_next_task()
        db.finish_attempt(
            task_id=task.id,
            role="builder",
            summary="ran",
            outcome="failed_verify" if index < failures else "pushed",
        )
    for index in range(turns):
        db.upsert_trace_run(
            TraceRunRow(
                trace_id=f"{index:032d}",
                turn_id="t1",
                source="voice",
                surface="voice_prompts",
                runner="",
                repo="",
                task_id=None,
                attempt_id=None,
                outcome="friction" if index < bad_turns else "ok",
                friction_kind="stopped" if index < bad_turns else None,
                causal_sig="",
                latency_ms=900,
                ttfb_ms=None,
                tokens_in=0,
                tokens_out=0,
                cost_usd=0.0,
                git_sha="master @ abc1234",
                prompt_sha="deadbeef",
                skills_used=(),
                user_input="hi",
                output_summary="ok",
                trace_path="",
                superseded_by=None,
                reflected=0,
            )
        )


def test_meta_utility_measures_pass_rate_and_friction(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    _seed_meta(db, tasks=4, failures=2, turns=10, bad_turns=3)
    metrics = db.compute_meta_utility()
    assert metrics["first_attempt_pass_rate"] == 0.5
    assert metrics["friction_per_100_turns"] == 30.0
    assert metrics["attempted_tasks"] == 4
    assert metrics["turns"] == 10


async def test_probation_trips_when_the_improver_gets_worse(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    rec = Reconciler(db, cfg, hub=TelemetryHub())
    healthy = {
        "first_attempt_pass_rate": 0.9,
        "friction_per_100_turns": 2.0,
        "median_usd_per_solved_task": 1.0,
        "median_turns_per_solved_task": 4.0,
    }
    db.start_improver_probation(
        rollback_sha="deadbeef", baseline_json=json.dumps(healthy), until_iso="2999-01-01T00:00:00Z"
    )
    _seed_meta(
        db,
        tasks=PROBATION_MIN_TASKS,
        failures=PROBATION_MIN_TASKS,  # every task missed its first attempt
        turns=PROBATION_MIN_TURNS,
        bad_turns=PROBATION_MIN_TURNS,
    )
    await rec._check_improver_probation()
    assert db.improver_probation() is None
    outbox = [row for row in db.list_outbox() if row.kind == "approval_request"]
    assert outbox and "roll back" in outbox[0].summary_spoken.lower()
    assert json.loads(outbox[0].details_json)["rollback_sha"] == "deadbeef"


async def test_probation_stays_quiet_without_enough_new_evidence(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    db.start_improver_probation(
        rollback_sha="deadbeef",
        baseline_json=json.dumps({"first_attempt_pass_rate": 0.9}),
        until_iso="2999-01-01T00:00:00Z",
    )
    _seed_meta(db, tasks=1, failures=1, turns=1, bad_turns=1)
    await Reconciler(db, cfg, hub=TelemetryHub())._check_improver_probation()
    assert db.improver_probation() is not None  # still watching
    assert not [row for row in db.list_outbox() if row.kind == "approval_request"]
