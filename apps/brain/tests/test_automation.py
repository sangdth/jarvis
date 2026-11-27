from __future__ import annotations

import asyncio
import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from brain import memory
from brain.config import Settings
from brain.db import BrainDB
from brain.reconciler import Reconciler
from brain.server import BrainServer


def _fake_pi(tmp_path: Path, *, cost: float = 0.01) -> Path:
    script = tmp_path / "fake_pi_check.py"
    script.write_text(
        f"""#!/usr/bin/env python3
import json, sys
prompt = sys.argv[-1]
assert "scheduled check" in prompt.lower(), prompt
print(json.dumps({{"type": "session", "id": "sess-check"}}), flush=True)
print(json.dumps({{"type": "message_end",
                  "message": {{"role": "assistant",
                               "content": "Two pull requests need you.\\nBOOKMARK gh-prs:jarvis=2026-11-22T08:00:00Z"}},
                  "cost_usd": {cost}}}), flush=True)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _cfg(tmp_path: Path, **extra: object) -> Settings:
    return Settings(
        home_dir=tmp_path / "home",
        projects_dir=tmp_path / "Projects",
        allowed_repos="jarvis",
        pi_bin=str(_fake_pi(tmp_path, cost=float(extra.pop("cost", 0.01)))),
        sandbox_enabled=False,
        brain_api_key="secret",
        **extra,
    )


def test_fire_due_followups_and_schedules(tmp_path: Path) -> None:
    db = BrainDB(tmp_path / "state.db", {"jarvis"})
    try:
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        one = db.add_timer(kind="followup", title="once", prompt_spec="check once", run_at=past)
        recurring = db.add_timer(
            kind="schedule", title="daily", prompt_spec="check daily", cron_expr="0 8 * * *",
            timezone_name="UTC",
        )
        fired = db.fire_due_timers()
        assert [t.id for t in fired] == [one.id]
        assert db.get_timer(one.id).status == "firing"
        db.finish_timer(one.id)
        assert db.get_timer(one.id).status == "done"

        # Move the schedule into the past and fire it: it advances and stays active.
        db._conn.execute(  # type: ignore[attr-defined]
            "UPDATE timers SET next_run_at = ? WHERE id = ?",
            ((past).strftime("%Y-%m-%dT%H:%M:%SZ"), recurring.id),
        )
        fired = db.fire_due_timers()
        assert [t.id for t in fired] == [recurring.id]
        advanced = db.get_timer(recurring.id)
        assert advanced.status == "firing"
        assert advanced.next_run_at > past.strftime("%Y-%m-%dT%H:%M:%SZ")
        db.finish_timer(recurring.id)
        assert db.get_timer(recurring.id).status == "active"
    finally:
        db.close()


def test_recover_firing_timer_after_crash(tmp_path: Path) -> None:
    db = BrainDB(tmp_path / "state.db", {"jarvis"})
    try:
        timer = db.add_timer(
            kind="followup", title="once", prompt_spec="check",
            run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        db.fire_due_timers()
        db._conn.execute(  # type: ignore[attr-defined]
            "UPDATE timers SET updated_at = '2000-01-01T00:00:00Z' WHERE id = ?", (timer.id,)
        )
        assert db.recover_firing_timers(max_age_seconds=60) == [timer.id]
        assert db.get_timer(timer.id).status == "active"
    finally:
        db.close()


def test_dedupe_key_returns_the_active_task(tmp_path: Path) -> None:
    db = BrainDB(tmp_path / "state.db", {"jarvis"})
    try:
        first = db.upsert_task(
            kind="problem", repo="jarvis", title="one", spec="s", verify_cmd="true",
            dedupe_key="timer:TM-1:slot", ready=True,
        )
        second = db.upsert_task(
            kind="problem", repo="jarvis", title="two", spec="s", verify_cmd="true",
            dedupe_key="timer:TM-1:slot", ready=True,
        )
        assert first.id == second.id
        assert len(db.list_tasks()) == 1
    finally:
        db.close()


def test_a_voice_run_does_not_duplicate_its_outbox_report(tmp_path: Path) -> None:
    db = BrainDB(tmp_path / "state.db", {"jarvis"})
    try:
        task = db.upsert_task(
            kind="problem", repo="jarvis", title="live", spec="s", verify_cmd="true", ready=True
        )
        claimed = db.claim_next_task(task_id=task.id)
        assert claimed is not None
        db.finish_attempt(
            task_id=task.id, role="builder", summary="done", outcome="pushed", notify_owner=False
        )
        assert db.list_outbox() == []

        background = db.upsert_task(
            kind="problem", repo="jarvis", title="night", spec="s", verify_cmd="true", ready=True
        )
        claimed = db.claim_next_task(task_id=background.id)
        assert claimed is not None
        db.finish_attempt(task_id=background.id, role="builder", summary="done", outcome="pushed")
        assert [row.kind for row in db.list_outbox()] == ["report"]
    finally:
        db.close()


async def test_timer_check_queues_an_outbox_report(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg)
    timer = db.add_timer(
        kind="followup", title="PR check", prompt_spec="check the pull requests",
        run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    try:
        await reconciler.tick()
        await reconciler.wait_for_timer_checks()
        assert db.get_timer(timer.id).status == "done"
        reports, approval = db.drain_digest()
        assert approval is None
        assert any("Two pull requests need you" in item.summary_spoken for item in reports)
        assert all("BOOKMARK" not in item.summary_spoken for item in reports)
        assert memory.read_bookmarks(cfg.home_dir)["gh-prs:jarvis"].startswith("2026-11-22")
    finally:
        await reconciler.stop()
        db.close()


async def test_reminder_fast_path_and_timer_api(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg)
    server = BrainServer(db, reconciler, cfg)
    headers = {"Authorization": "Bearer secret"}
    try:
        async with TestClient(TestServer(server.app)) as client:
            resp = await client.post(
                "/v1/ask", json={"input": "remind me to stretch in 30 minutes"}, headers=headers
            )
            assert resp.status == 200
            spoken = (await resp.json())["spoken"]
            assert "check back in 30 minutes" in spoken
            timers = (await (await client.get("/v1/timers", headers=headers)).json())["timers"]
            assert len(timers) == 1
            assert timers[0]["kind"] == "followup"
            assert timers[0]["action"] == "check"

            check_in = await client.post(
                "/v1/ask", json={"input": "check in 20 minutes"}, headers=headers
            )
            assert check_in.status == 200
            assert "check back in 20 minutes" in (await check_in.json())["spoken"]

            created = await client.post(
                "/v1/timers",
                json={"every": "0 8 * * 1-5", "title": "Morning PRs", "prompt": "check the PRs"},
                headers=headers,
            )
            assert created.status == 200
            timer = (await created.json())["timer"]
            assert timer["kind"] == "schedule"
            assert timer["cron_expr"] == "0 8 * * 1-5"

            cancelled = await client.post(f"/v1/timers/{timer['id']}/cancel", headers=headers)
            assert cancelled.status == 200
            assert (await cancelled.json())["timer"]["status"] == "done"

            queued = db.queue_outbox(kind="report", summary_spoken="A background task finished.")
            rows = (await (await client.get("/v1/outbox", headers=headers)).json())["outbox"]
            assert any(row["id"] == queued.id for row in rows)
            acked = await client.post(f"/v1/outbox/{queued.id}/spoken", headers=headers)
            assert acked.status == 200
            assert db.get_outbox(queued.id).status == "spoken"  # type: ignore[union-attr]
            assert "A background task finished." in memory.reported_path(cfg.home_dir).read_text()
    finally:
        await reconciler.stop()
        db.close()


async def test_timer_task_action_queues_a_board_task(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg)
    try:
        followup = db.add_timer(
            kind="followup", action="task", title="Nightly build", prompt_spec="build it",
            verify_cmd="true", run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        schedule = db.add_timer(
            kind="schedule", action="task", title="Daily build", prompt_spec="build it",
            verify_cmd="true", cron_expr="0 8 * * *", timezone_name="UTC",
        )
        db._conn.execute(  # type: ignore[attr-defined]
            "UPDATE timers SET next_run_at = '2000-01-01T00:00:00Z' WHERE id = ?", (schedule.id,)
        )
        await reconciler._fire_timers()
        tasks = db.list_tasks()
        assert {task.title for task in tasks} == {"Nightly build", "Daily build"}
        assert all(task.dedupe_key for task in tasks)
        assert db.get_timer(followup.id).status == "done"
        assert db.get_timer(schedule.id).status == "active"
    finally:
        await reconciler.stop()
        db.close()


async def test_assist_outbox_is_published_on_the_events_stream(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg)
    server = BrainServer(db, reconciler, cfg)
    headers = {"Authorization": "Bearer secret"}
    try:
        async with TestClient(TestServer(server.app)) as client:
            async with client.get("/v1/events", headers=headers) as stream:
                await asyncio.sleep(0)
                queued = db.queue_outbox(kind="report", summary_spoken="Background work is done.")
                found: dict | None = None
                for _ in range(20):
                    line = await asyncio.wait_for(stream.content.readline(), timeout=2.0)
                    text = line.decode("utf-8").strip()
                    if not text.startswith("data:"):
                        continue
                    event = json.loads(text.removeprefix("data:").strip())
                    if event.get("event") == "outbox.queued":
                        found = event["outbox"]
                        break
                assert found is not None
                assert found["id"] == queued.id
                assert found["summary_spoken"] == "Background work is done."
    finally:
        await reconciler.stop()
        db.close()


async def test_timer_api_rejects_two_whens(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg)
    server = BrainServer(db, reconciler, cfg)
    try:
        async with TestClient(TestServer(server.app)) as client:
            resp = await client.post(
                "/v1/timers",
                json={"in": "30m", "every": "0 8 * * *", "title": "x"},
                headers={"Authorization": "Bearer secret"},
            )
            assert resp.status == 400
    finally:
        db.close()


async def test_cancelled_followup_timer_stays_active_and_startup_reaps_orphaned_tasks(
    tmp_path: Path,
) -> None:
    slow_pi = tmp_path / "slow_pi.py"
    slow_pi.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n", encoding="utf-8")
    slow_pi.chmod(slow_pi.stat().st_mode | stat.S_IEXEC)
    cfg = _cfg(tmp_path)
    cfg = cfg.model_copy(update={"pi_bin": str(slow_pi)})
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg)
    try:
        timer = db.add_timer(
            kind="followup", title="Remind me", prompt_spec="check",
            run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        await reconciler._fire_timers()
        await asyncio.sleep(0.05)  # let the check subprocess start before shutdown cancels it
        assert db.get_timer(timer.id).status == "firing"
        await reconciler.stop()
        # Cancelling on shutdown must return the one-shot reminder to `active`, never mark it `done`.
        assert db.get_timer(timer.id).status == "active"

        # A task left `in_progress` by a killed daemon is reaped immediately on `Reconciler.start()`.
        task = db.upsert_task(
            kind="problem", repo="jarvis", title="Interrupted", spec="s", verify_cmd="true", ready=True
        )
        assert db.claim_next_task(task_id=task.id, lease_seconds=1200) is not None
        assert db.get_task(task.id).status == "in_progress"
        reconciler2 = Reconciler(db, cfg)
        reconciler2.start()
        try:
            assert db.get_task(task.id).status == "ready"
        finally:
            await reconciler2.stop()
    finally:
        db.close()
