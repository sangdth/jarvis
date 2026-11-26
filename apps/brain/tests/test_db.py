from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brain.db import BrainDB


def test_allowed_repos_blocks_hamahakki_and_traversal(tmp_path: Path) -> None:
    db = BrainDB(tmp_path / "state.db", allowed_repos={"jarvis"})
    try:
        assert db.get_state().daily_budget_usd == 200.0
        with pytest.raises(ValueError, match="not in ALLOWED_REPOS"):
            db.upsert_task(
                kind="problem",
                repo="hamahakki",
                title="Touch crab robot",
                spec="Should never be allowed",
            )
        with pytest.raises(ValueError, match="Invalid repository path"):
            db.upsert_task(
                kind="problem",
                repo="../hamahakki",
                title="Traversal",
                spec="Blocked",
            )
    finally:
        db.close()


def test_goal_requires_metric_target(tmp_path: Path) -> None:
    db = BrainDB(tmp_path / "state.db", allowed_repos={"jarvis"})
    try:
        with pytest.raises(ValueError, match="metric_target"):
            db.upsert_task(
                kind="goal",
                repo="jarvis",
                title="Vague goal",
                spec="No metric",
            )
        task = db.upsert_task(
            kind="goal",
            repo="jarvis",
            title="Cut latency",
            spec="Optimize filter",
            metric_target="p95_ms < 200",
        )
        assert task.id == "SG-1"
        assert task.status == "draft"  # ASSIST mode default
    finally:
        db.close()


def test_claim_priority_repo_lock_and_three_strikes(tmp_path: Path) -> None:
    db = BrainDB(
        tmp_path / "state.db",
        allowed_repos={"jarvis", "opscom/web"},
        default_mode="AUTONOMOUS",
    )
    try:
        sg = db.upsert_task(
            kind="goal",
            repo="jarvis",
            title="Goal 1",
            spec="Spec",
            metric_target="pass",
            verify_cmd="pytest",
            priority=10,
        )
        prb = db.upsert_task(
            kind="problem",
            repo="jarvis",
            title="Problem 1",
            spec="Crash",
            verify_cmd="pytest",
            priority=50,
        )
        # Problem is claimed before Goal even with higher numeric priority value
        claimed = db.claim_next_task()
        assert claimed is not None
        assert claimed.id == prb.id
        assert claimed.status == "in_progress"

        # Same repo ("jarvis") is locked while PRB-1 is in_progress
        assert db.claim_next_task() is None

        # 3 failed attempts -> blocked + outbox blocker queued
        for i in range(3):
            updated = db.finish_attempt(
                task_id=prb.id,
                role="builder",
                summary=f"Try {i + 1}",
                outcome="failed_verify",
                cost_usd=1.25,
                verify_output="1 failed",
            )
            if i < 2:
                assert updated.status == "ready"
                assert updated.strike_count == i + 1
                reclaimed = db.claim_next_task()
                assert reclaimed is not None and reclaimed.id == prb.id
            else:
                assert updated.status == "blocked"
                assert updated.strike_count == 3

        # Now SG-1 in repo "jarvis" can be claimed
        next_claimed = db.claim_next_task()
        assert next_claimed is not None and next_claimed.id == sg.id

        reports, approval = db.drain_digest()
        assert approval is None
        assert len(reports) == 1
        assert reports[0].kind == "blocker"
        assert "blocked after 3 failed attempts" in reports[0].summary_spoken
        assert abs(db.get_state().spent_today_usd - 3.75) < 1e-6
    finally:
        db.close()


def test_stale_lease_reaper(tmp_path: Path) -> None:
    db = BrainDB(tmp_path / "state.db", allowed_repos={"jarvis"}, default_mode="AUTONOMOUS")
    try:
        t = db.upsert_task(
            kind="problem",
            repo="jarvis",
            title="Hang",
            spec="Spec",
            verify_cmd="true",
        )
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        claimed = db.claim_next_task(lease_seconds=30, now=past)
        assert claimed is not None and claimed.id == t.id

        reaped = db.reap_stale_leases(now=datetime.now(timezone.utc))
        assert reaped == [t.id]
        refreshed = db.get_task(t.id)
        assert refreshed.status == "ready"
        assert refreshed.strike_count == 0
        assert db.list_attempts(t.id)[0].outcome == "reaped"
    finally:
        db.close()


def test_in_progress_guard_and_parent_goal_completion(tmp_path: Path) -> None:
    db = BrainDB(tmp_path / "state.db", allowed_repos={"jarvis"}, default_mode="AUTONOMOUS")
    try:
        parent = db.upsert_task(
            kind="goal",
            repo="jarvis",
            title="Parent goal",
            spec="Decompose me",
            metric_target="all children pass",
        )
        claimed_parent = db.claim_next_task()
        assert claimed_parent is not None and claimed_parent.id == parent.id

        with pytest.raises(ValueError, match="in_progress"):
            db.upsert_task(
                task_id=parent.id,
                kind="goal",
                repo="jarvis",
                title="Parent goal",
                spec="Decompose me",
                metric_target="all children pass",
                ready=False,
            )

        child = db.upsert_task(
            task_id="SG-1.1",
            parent_id=parent.id,
            kind="goal",
            repo="jarvis",
            title="Child goal",
            spec="Build feature",
            metric_target="pytest passes",
            verify_cmd="pytest",
            ready=True,
        )
        after_plan = db.finish_attempt(
            task_id=parent.id,
            role="planner",
            summary="Created SG-1.1",
            outcome="decomposed",
        )
        # Parent stays open until child finishes
        assert after_plan.status == "draft"

        claimed_child = db.claim_next_task()
        assert claimed_child is not None and claimed_child.id == child.id
        db.finish_attempt(
            task_id=child.id,
            role="builder",
            summary="Built child",
            outcome="pushed",
            git_branch="brain/SG-1.1",
            git_sha="abc1234",
        )
        assert db.get_task(parent.id).status == "done"
    finally:
        db.close()


def test_cli_add_status_and_launchd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from brain import cli

    test_cfg = cli.settings.model_copy(
        update={
            "home_dir": tmp_path / "home",
            "projects_dir": tmp_path / "Projects",
            "allowed_repos": "jarvis",
        }
    )
    monkeypatch.setattr(cli, "settings", test_cfg)

    assert (
        cli.main(
            [
                "add",
                "--kind",
                "problem",
                "--repo",
                "jarvis",
                "--title",
                "CLI bug",
                "--spec",
                "Stacktrace",
                "--verify",
                "pytest",
                "--ready",
            ]
        )
        == 0
    )
    assert "PRB-1" in capsys.readouterr().out

    assert cli.main(["status"]) == 0
    status_out = capsys.readouterr().out
    assert "PRB-1" in status_out
    assert "$200.00" in status_out

    assert cli.main(["launchd"]) == 0
    plist_out = capsys.readouterr().out
    assert "com.sangdth.jarvis" in plist_out
    assert "jarvis.cli" in plist_out  # the LaunchAgent runs the one command, which starts the brain


def test_task_mode_inheritance_and_mode_filtered_claiming(tmp_path: Path) -> None:
    db = BrainDB(tmp_path / "state.db", allowed_repos={"jarvis", "demo"}, default_mode="ASSIST")
    try:
        assist_parent = db.upsert_task(
            kind="goal",
            mode="ASSIST",
            repo="jarvis",
            title="Assist parent",
            spec="Related to active topic",
            metric_target="pytest passes",
            ready=True,
        )
        assert assist_parent.mode == "ASSIST"

        # Child inherits parent's ASSIST mode even if system_state switches to AUTONOMOUS
        db.set_mode("AUTONOMOUS")
        assist_child = db.upsert_task(
            task_id="SG-1.1",
            parent_id=assist_parent.id,
            kind="goal",
            repo="jarvis",
            title="Assist child",
            spec="Step 1",
            metric_target="pytest passes",
            verify_cmd="pytest",
            ready=True,
        )
        assert assist_child.mode == "ASSIST"

        auto_task = db.upsert_task(
            kind="problem",
            mode="AUTONOMOUS",
            repo="demo",
            title="Overnight cleanup",
            spec="Resolve overnight",
            verify_cmd="pytest",
            ready=True,
        )
        assert auto_task.mode == "AUTONOMOUS"

        assert [t.id for t in db.list_tasks(mode="ASSIST")] == ["SG-1", "SG-1.1"]
        assert [t.id for t in db.list_tasks(mode="AUTONOMOUS")] == ["PRB-1"]

        # In ASSIST mode, claim_next_task(mode="ASSIST") only claims ASSIST tasks
        claimed_assist = db.claim_next_task(mode="ASSIST")
        assert claimed_assist is not None and claimed_assist.id == "SG-1"
        db.finish_attempt(task_id="SG-1", role="planner", summary="Decomposed", outcome="decomposed")

        # In AUTONOMOUS mode, claim_next_task(mode="AUTONOMOUS") only claims AUTONOMOUS tasks
        claimed_auto = db.claim_next_task(mode="AUTONOMOUS")
        assert claimed_auto is not None and claimed_auto.id == "PRB-1"
        assert db.claim_next_task(mode="AUTONOMOUS") is None
    finally:
        db.close()


def test_upsert_task_rejects_path_traversal_and_fts_handles_punctuation(tmp_path: Path) -> None:
    import pytest

    from brain.db import TraceRunRow

    db = BrainDB(tmp_path / "state.db", allowed_repos={"jarvis"})
    try:
        for bad_id in ("../escape", "../../etc", "SG-1/../../x", ".hidden", "a/b"):
            with pytest.raises(ValueError, match="Invalid task id"):
                db.upsert_task(
                    task_id=bad_id,
                    kind="problem",
                    repo="jarvis",
                    title="Bad id",
                    spec="Attempt traversal",
                )
        valid = db.upsert_task(
            task_id="SG-9.1",
            kind="goal",
            repo="jarvis",
            title="Valid id",
            spec="Sub-hypothesis",
            metric_target="passes",
        )
        assert valid.id == "SG-9.1"

        db.upsert_trace_run(
            TraceRunRow(
                trace_id="t" * 32,
                turn_id="t1",
                source="voice",
                surface="brain_router",
                runner="t3",
                repo="jarvis",
                task_id=None,
                attempt_id=None,
                outcome="ok",
                friction_kind=None,
                causal_sig="",
                latency_ms=100,
                ttfb_ms=None,
                tokens_in=0,
                tokens_out=0,
                cost_usd=0.0,
                git_sha="abc",
                prompt_sha="def",
                skills_used=(),
                user_input="check foo-bar reconnect",
                output_summary="done",
                trace_path="",
                superseded_by=None,
                reflected=0,
            )
        )
        assert len(db.list_trace_runs(query='foo-bar "reconnect')) == 1
        assert db.list_trace_runs(query='"" : * -') == []
    finally:
        db.close()
