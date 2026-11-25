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
    # Plain decision lines do NOT count as corrections.
    feedback.write_text("\n".join(json.dumps({"id": f"d{i}"}) for i in range(40)) + "\n", encoding="utf-8")
    ready, why = laya_intent_ready(home)
    assert not ready and "0 new corrections" in why
    # 10 corrections + 40 decisions is still under the threshold.
    lines = [json.dumps({"id": f"d{i}"}) for i in range(40)] + [
        json.dumps({"correction_of": f"d{i}", "label": "ADDRESSED_TO_JARVIS"}) for i in range(10)
    ]
    feedback.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ready, why = laya_intent_ready(home)
    assert not ready and "10 new corrections" in why
    # 25 corrections crosses the threshold.
    lines = [json.dumps({"id": f"d{i}"}) for i in range(40)] + [
        json.dumps({"correction_of": f"d{i}", "label": "ADDRESSED_TO_JARVIS"}) for i in range(25)
    ]
    feedback.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert laya_intent_ready(home)[0]
    assert not laya_intent_ready(home, bookmark_cursor=45)[0]


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


async def test_approving_probation_rollback_resets_git_and_sets_restart_pending(tmp_path: Path) -> None:
    import subprocess

    from aiohttp.test_utils import TestClient, TestServer

    from brain.server import BrainServer

    repo = tmp_path / "jarvis_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "master", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tester"], check=True)
    (repo / "improver.txt").write_text("good\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "improver.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "good"], check=True, capture_output=True)
    sha_good = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    (repo / "improver.txt").write_text("regressed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "improver.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "bad"], check=True, capture_output=True)
    sha_bad = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    assert sha_good != sha_bad

    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=tmp_path / "Projects",
        self_repo_dir=repo,
        allowed_repos="jarvis",
        brain_api_key="secret-token",
        sandbox_enabled=False,
    )
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    rec = Reconciler(db, cfg, hub=TelemetryHub())
    db.start_improver_probation(
        rollback_sha=sha_good,
        baseline_json=json.dumps(
            {
                "first_attempt_pass_rate": 0.9,
                "friction_per_100_turns": 2.0,
                "median_usd_per_solved_task": 1.0,
            }
        ),
        until_iso="2999-01-01T00:00:00Z",
    )
    _seed_meta(
        db,
        tasks=PROBATION_MIN_TASKS,
        failures=PROBATION_MIN_TASKS,
        turns=PROBATION_MIN_TURNS,
        bad_turns=PROBATION_MIN_TURNS,
    )
    await rec._check_improver_probation()
    approvals = [row for row in db.list_outbox() if row.kind == "approval_request"]
    assert len(approvals) == 1
    assert approvals[0].task_id is None

    server = BrainServer(db, rec, cfg)
    headers = {"Authorization": "Bearer secret-token"}
    async with TestClient(TestServer(server.app)) as client:
        # Approve through the live morning brief flow.
        brief_resp = await client.post("/v1/runs", json={"input": "morning brief"}, headers=headers)
        run_id = (await brief_resp.json())["run_id"]
        async with client.get(f"/v1/runs/{run_id}/events", headers=headers) as sse:
            async for raw_line in sse.content:
                line = raw_line.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line.removeprefix("data:").strip())
                if ev.get("event") == "approval.request":
                    ans = await client.post(
                        f"/v1/runs/{run_id}/answer",
                        json={"answer": "yes", "request_id": ev["request_id"]},
                        headers=headers,
                    )
                    assert ans.status == 200
                elif ev.get("event") == "run.completed":
                    assert sha_good[:7] in ev["output"]
                    break

    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == sha_good
    assert rec.restart_pending is True
    await rec.stop()
    db.close()
