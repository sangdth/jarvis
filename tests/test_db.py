from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from superbrain.db import BrainDB


def test_allowed_repos_blocks_hamahakki_and_traversal(tmp_path: Path) -> None:
    db = BrainDB(tmp_path / "state.db", allowed_repos={"jarvis"})
    try:
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
        assert refreshed.strike_count == 1
    finally:
        db.close()
