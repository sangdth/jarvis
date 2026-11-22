from __future__ import annotations

from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from brain.config import Settings
from brain.db import BrainDB
from brain.reconciler import Reconciler
from brain.server import BrainServer
from brain.telemetry import (
    EVENT_BOARD_RPC,
    EVENT_HTTP_INBOUND,
    TelemetryHub,
    preview,
)


def _make_brain(tmp_path: Path, hub: TelemetryHub | None = None) -> tuple[BrainServer, BrainDB]:
    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=tmp_path / "Projects",
        allowed_repos="jarvis",
        brain_api_key="secret-token",
        sandbox_enabled=False,
    )
    (cfg.projects_dir / "jarvis" / ".git").mkdir(parents=True, exist_ok=True)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    shared_hub = hub or TelemetryHub()
    reconciler = Reconciler(db, cfg, hub=shared_hub)
    return BrainServer(db, reconciler, cfg, hub=shared_hub), db


def test_preview_collapses_and_truncates() -> None:
    assert preview(None) == ""
    assert preview("  a\n b\t c ") == "a b c"
    long = "x" * 500
    assert len(preview(long, 40)) == 40
    assert preview(long, 40).endswith("…")


def test_hub_ring_buffer_and_fanout() -> None:
    hub = TelemetryHub(max_events=3)
    for i in range(5):
        hub.emit(EVENT_BOARD_RPC, method=f"m{i}")
    snapshot = hub.snapshot(10)
    assert [e["method"] for e in snapshot] == ["m2", "m3", "m4"]
    assert snapshot[-1]["seq"] == 5
    assert "ts" in snapshot[-1]

    queue = hub.subscribe()
    assert hub.subscriber_count == 1
    hub.emit(EVENT_BOARD_RPC, method="live")
    assert queue.get_nowait()["method"] == "live"
    hub.unsubscribe(queue)
    assert hub.subscriber_count == 0
    hub.emit(EVENT_BOARD_RPC, method="after-unsubscribe")
    assert queue.empty()


async def test_telemetry_snapshot_endpoint_reports_inbound_events(tmp_path: Path) -> None:
    hub = TelemetryHub()
    brain_server, db = _make_brain(tmp_path, hub)
    try:
        async with TestClient(TestServer(brain_server.app)) as client:
            headers = {"Authorization": "Bearer secret-token"}
            unauthorized = await client.get("/v1/telemetry/snapshot")
            assert unauthorized.status == 401

            asked = await client.post(
                "/v1/ask", json={"input": "Switch to assist mode"}, headers=headers
            )
            assert asked.status == 200

            resp = await client.get("/v1/telemetry/snapshot?limit=20", headers=headers)
            assert resp.status == 200
            payload = await resp.json()
            assert payload["mode"] == "ASSIST"
            events = [e["event"] for e in payload["events"]]
            assert EVENT_HTTP_INBOUND in events
            inbound = next(e for e in payload["events"] if e["event"] == EVENT_HTTP_INBOUND)
            assert inbound["source"] == "iphone_ask"
    finally:
        db.close()


async def test_outbox_resolve_endpoint_approves_and_requeues_task(tmp_path: Path) -> None:
    hub = TelemetryHub()
    brain_server, db = _make_brain(tmp_path, hub)
    try:
        task = db.upsert_task(
            kind="problem",
            repo="jarvis",
            title="Protected upgrade",
            spec="Needs owner approval",
            verify_cmd="true",
            ready=False,
        )
        assert task.status == "draft"
        outbox = db.queue_outbox(
            kind="approval_request",
            task_id=task.id,
            summary_spoken="Approve protected files?",
            details={"approved_sha": "deadbeef"},
        )

        async with TestClient(TestServer(brain_server.app)) as client:
            headers = {"Authorization": "Bearer secret-token"}
            missing = await client.post(
                "/v1/outbox/OB-nope/resolve", json={"approved": True}, headers=headers
            )
            assert missing.status == 404

            resolved = await client.post(
                f"/v1/outbox/{outbox.id}/resolve", json={"approved": True}, headers=headers
            )
            assert resolved.status == 200
            body = await resolved.json()
            assert body["status"] == "approved"
            assert body["task_id"] == task.id

        assert db.get_task(task.id).status == "ready"
        assert db.get_unconsumed_approved_outbox(task.id) is not None
    finally:
        db.close()
