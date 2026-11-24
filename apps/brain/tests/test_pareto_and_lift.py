from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from brain.config import Settings
from brain.db import BrainDB
from brain.pareto import Candidate, parse_metrics, select_winner
from brain.skills import has_no_lift, skill_lift


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        home_dir=tmp_path / "home",
        projects_dir=tmp_path / "Projects",
        allowed_repos="demo",
        sandbox_enabled=False,
    )


def test_a_tiny_accuracy_gain_does_not_buy_voice_latency() -> None:
    baseline = Candidate("baseline", accuracy=0.95, p50_ms=600.0, cost_usd=0.01)
    slower = Candidate("slower", accuracy=0.955, p50_ms=780.0, cost_usd=0.01)
    assert select_winner([slower], baseline=baseline) is None


def test_a_real_accuracy_gain_earns_its_latency() -> None:
    baseline = Candidate("baseline", accuracy=0.90, p50_ms=600.0, cost_usd=0.01)
    better = Candidate("better", accuracy=0.96, p50_ms=700.0, cost_usd=0.01)
    winner = select_winner([better], baseline=baseline)
    assert winner is not None and winner.id == "better"


def test_a_candidate_that_regressed_a_sealed_case_is_out() -> None:
    baseline = Candidate("baseline", accuracy=0.90, p50_ms=600.0, cost_usd=0.01)
    regressing = Candidate("regressing", accuracy=0.99, p50_ms=400.0, cost_usd=0.0, regressions=1)
    assert select_winner([regressing], baseline=baseline) is None


def test_an_equal_candidate_earns_nothing() -> None:
    baseline = Candidate("baseline", accuracy=0.95, p50_ms=600.0, cost_usd=0.01)
    equal = Candidate("equal", accuracy=0.95, p50_ms=600.0, cost_usd=0.01)
    assert select_winner([equal], baseline=baseline) is None


def test_metrics_are_read_from_the_last_benchmark_line() -> None:
    output = "noise\nMETRICS: {\"accuracy\": 0.5, \"p50_ms\": 900}\nmore\nMETRICS: {\"accuracy\": 0.97, \"p50_ms\": 450}\n"
    assert parse_metrics(output) == {"accuracy": 0.97, "p50_ms": 450.0}
    assert parse_metrics("no metrics here") == {}
    assert parse_metrics(None) == {}


def test_a_skill_with_no_lift_against_the_baseline_is_retirable(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    task = db.upsert_task(
        kind="problem", repo="demo", title="Fix tests", spec="S", verify_cmd="pytest", ready=True
    )
    db.claim_next_task()
    for index in range(10):
        db.finish_attempt(
            task_id=task.id,
            role="builder",
            summary=f"with skill {index}",
            outcome="pushed" if index < 5 else "failed_verify",
            skills_used=["demo-skill"],
        )
    for index in range(5):
        db.claim_next_task()
        db.finish_attempt(
            task_id=task.id,
            role="builder",
            summary=f"baseline {index}",
            outcome="pushed" if index < 5 else "failed_verify",
        )
    lift = skill_lift(db, "demo-skill")
    assert lift["loads"] == 10
    assert lift["baseline"] == 5
    assert lift["pass_lift"] < 0  # half pass with the skill, all pass without it: worse, not better
    assert has_no_lift(
        db, SimpleNamespace(slug="demo-skill", loads_since_update=10, successes_since_update=5)
    )


def test_a_skill_that_helps_keeps_its_place(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    task = db.upsert_task(
        kind="problem", repo="demo", title="Fix tests", spec="S", verify_cmd="pytest", ready=True
    )
    db.claim_next_task()
    for index in range(10):
        db.finish_attempt(
            task_id=task.id, role="builder", summary=f"with {index}", outcome="pushed",
            skills_used=["demo-skill"],
        )
    for index in range(4):
        db.claim_next_task()
        db.finish_attempt(task_id=task.id, role="builder", summary=f"without {index}", outcome="failed_verify")
    lift = skill_lift(db, "demo-skill")
    assert lift["pass_lift"] > 0
    assert not has_no_lift(
        db, SimpleNamespace(slug="demo-skill", loads_since_update=10, successes_since_update=10)
    )


def test_the_evolvable_prompts_are_marked_as_blocks() -> None:
    from brain.orchestrator import ROUTER_PROMPT
    from jarvis_voice.prompts import CLEANUP_PROMPT, WAITING_PROMPT

    for name, prompt in (
        ("router", ROUTER_PROMPT),
        ("cleanup", CLEANUP_PROMPT),
        ("waiting", WAITING_PROMPT),
    ):
        assert f"# === EVOLVE-BLOCK: {name} ===" in prompt
        assert f"# === END-EVOLVE-BLOCK: {name} ===" in prompt
