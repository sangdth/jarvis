from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from brain.config import Settings
from brain.db import BrainDB, TraceRunRow
from brain.telemetry import TelemetryHub
from brain.traces import (
    HARNESS_SURFACES,
    TraceSink,
    causal_signature,
    classify_surface,
    distill_experience,
    sanitize_event,
    word_change_ratio,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        home_dir=tmp_path / "home",
        projects_dir=tmp_path / "Projects",
        allowed_repos="jarvis",
        brain_api_key="secret-token",
        sandbox_enabled=False,
    )


def _trace_row(trace_id: str, **overrides: object) -> TraceRunRow:
    base: dict[str, object] = dict(
        trace_id=trace_id,
        turn_id="t1",
        source="voice",
        surface="brain_router",
        runner="t3",
        repo="huggerhustle/platform-v2",
        task_id=None,
        attempt_id=None,
        outcome="friction",
        friction_kind="router_override",
        causal_sig="wrong_target|t3|router_disambiguation",
        latency_ms=1800,
        ttfb_ms=None,
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        git_sha="master @ abc1234",
        prompt_sha="deadbeef",
        skills_used=(),
        user_input="fix the login screen",
        output_summary="sent to the wrong project",
        trace_path="2026-11-24/trace-x.json",
        superseded_by=None,
        reflected=0,
    )
    base.update(overrides)
    return TraceRunRow(**base)  # type: ignore[arg-type]


def test_sanitize_drops_persistent_noise_and_redacts_pairing_codes(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-live-abcdefghijklmnop")
    assert sanitize_event({"event": "voice.level", "peak": 0.4}) is None
    assert sanitize_event({"event": "heartbeat", "uptime_s": 3}) is None
    pair = sanitize_event({"event": "pair.requested", "code": "123456", "name": "MacBook"})
    assert pair is not None and pair["code"] == "[redacted]"
    stt = sanitize_event({"event": "voice.stt", "text": "use sk-live-abcdefghijklmnop now"})
    assert stt is not None and "sk-live" not in stt["text"]
    room = sanitize_event({"event": "voice.stt", "text": "pass the salt", "room": True})
    assert room is None
    rejected = sanitize_event({"event": "voice.stt", "text": "background chatter", "passed": False})
    assert rejected is None
    accepted = sanitize_event({"event": "voice.stt", "text": "jarvis status", "passed": True, "room": False})
    assert accepted is not None and accepted["text"] == "jarvis status"


def test_cleanup_rewrite_ratio_and_surface_attribution() -> None:
    assert word_change_ratio("fix the login bug", "fix the login bug") == 0.0
    assert word_change_ratio("open the platform one", "open the jarvis project") > 0.35
    surface, sig = classify_surface("failed_verify")
    assert surface == "builder_harness" and sig.startswith("failed_verify|")
    surface, _ = classify_surface("errored", repeated_error_tools=("bash",))
    assert surface == "builder_harness"
    assert classify_surface("pushed") == ("", "")
    assert causal_signature("a", "b", "c") == "a|b|c"


async def _settle(predicate, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_trace_sink_writes_one_artifact_and_one_row(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-secret-value-123")
    cfg = _settings(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    hub = TelemetryHub()
    sink = TraceSink(db, cfg, hub)
    sink.start()
    trace_id = "a" * 32
    try:
        hub.emit("voice.stt", turn_id="t1", trace_id=trace_id, text="fix the login bug")
        hub.emit(
            "voice.intent", turn_id="t1", trace_id=trace_id, intent="ADDRESSED_TO_JARVIS", ms=90
        )
        hub.emit(
            "voice.cleanup", turn_id="t1", trace_id=trace_id, text="fix the login bug", changed=False
        )
        hub.emit(
            "voice.dispatch", turn_id="t1", trace_id=trace_id, run_id="run-1", text="fix the login bug"
        )
        hub.emit("run.status", run_id="run-1", trace_id=trace_id, turn_id="t1", status="running")
        hub.emit(
            "brain.route",
            phase="route",
            choice="delegate_project",
            runner="t3",
            project="huggerhustle/platform-v2",
            route_ms=240,
            trace_id=trace_id,
            turn_id="t1",
        )
        hub.emit("run.status", run_id="run-1", trace_id=trace_id, turn_id="t1", status="completed")
        hub.emit(
            "voice.tts",
            turn_id="t1",
            trace_id=trace_id,
            text="Done, the login is fixed and the key is eleven-secret-value-123.",
        )
        assert await _settle(lambda: db.get_trace_run(trace_id) is not None)
    finally:
        await sink.stop()

    row = db.get_trace_run(trace_id)
    assert row is not None
    assert row.outcome == "ok" and row.turn_id == "t1"
    assert row.runner == "t3" and row.repo == "huggerhustle/platform-v2"
    assert row.git_sha and row.prompt_sha
    artifact = cfg.traces_dir / row.trace_path
    assert artifact.is_file()
    record = json.loads(artifact.read_text(encoding="utf-8"))
    assert record["voice"]["stt"] == "fix the login bug"
    assert record["brain"]["route_ms"] == 240
    # Neither the API key value nor an unredacted credential may reach disk.
    for path in cfg.traces_dir.rglob("*"):
        if path.is_file():
            assert "eleven-secret-value-123" not in path.read_text(encoding="utf-8", errors="replace")


async def test_replace_and_stop_back_patch_the_row(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    db.upsert_trace_run(_trace_row("f" * 32, outcome="ok", friction_kind=None, causal_sig=""))
    sink = TraceSink(db, cfg, TelemetryHub())
    sink.mark_replaced("f" * 32, "e" * 32, old_runner="t3", new_runner="claude")
    row = db.get_trace_run("f" * 32)
    assert row is not None
    assert row.friction_kind == "replaced" and row.outcome == "friction"
    assert row.superseded_by == "e" * 32 and row.surface == "brain_router"
    db.upsert_trace_run(_trace_row("d" * 32, outcome="ok", friction_kind=None, causal_sig=""))
    sink.mark_stopped("d" * 32)
    stopped = db.get_trace_run("d" * 32)
    assert stopped is not None and stopped.friction_kind == "stopped" and stopped.outcome == "cancelled"


def test_router_override_detects_a_retry_that_adds_a_project(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    sink = TraceSink(db, cfg, TelemetryHub())
    db.upsert_trace_run(_trace_row("1" * 32, outcome="ok", friction_kind=None, causal_sig=""))
    sink.note_run_created(
        trace_id="1" * 32,
        turn_id="t1",
        session_id="s1",
        user_input="fix the login bug",
        project="",
        runner="t3",
        replace=False,
    )
    sink.note_run_created(
        trace_id="2" * 32,
        turn_id="t2",
        session_id="s1",
        user_input="no, fix the login bug in platform v2",
        project="huggerhustle/platform-v2",
        runner="t3",
        replace=False,
    )
    row = db.get_trace_run("1" * 32)
    assert row is not None and row.friction_kind == "router_override"
    assert row.superseded_by == "2" * 32


def test_fts_search_and_digest(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    db.upsert_trace_run(_trace_row("a" * 32, user_input="deploy the payments service"))
    db.upsert_trace_run(
        _trace_row(
            "b" * 32,
            outcome="failed_verify",
            friction_kind="attempt_failed_verify",
            surface="builder_harness",
            user_input="fix the flaky websocket reconnect",
            superseded_by=None,
        )
    )
    hits = db.list_trace_runs(query="websocket")
    assert [row.trace_id for row in hits] == ["b" * 32]
    overview = distill_experience(db, cfg)
    text = overview.read_text(encoding="utf-8")
    assert "Trace digest" in text
    assert "builder_harness" in text
    for surface in HARNESS_SURFACES:
        assert (cfg.traces_dir / "analysis" / "surfaces" / f"{surface}.md").is_file()


async def test_traces_routes_are_owner_scoped(tmp_path: Path) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from brain.reconciler import Reconciler
    from brain.server import BrainServer

    cfg = _settings(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    hub = TelemetryHub()
    server = BrainServer(db, Reconciler(db, cfg, hub=hub), cfg, hub=hub)
    db.upsert_trace_run(_trace_row("a" * 32))
    headers = {"Authorization": "Bearer secret-token"}
    async with TestClient(TestServer(server.app)) as client:
        assert (await client.get("/v1/traces")).status == 401
        body = await (await client.get("/v1/traces", headers=headers)).json()
        assert body["traces"][0]["trace_id"] == "a" * 32
        detail = await (await client.get(f"/v1/traces/{'a' * 32}", headers=headers)).json()
        assert detail["trace"]["surface"] == "brain_router"
        diff = await (await client.get("/v1/traces/diff", headers=headers)).json()
        assert diff["stats"][0]["runs"] == 1
        missing = await client.get(f"/v1/traces/{'0' * 32}", headers=headers)
        assert missing.status == 404


async def test_nightly_reflection_fires_in_assist_mode(tmp_path: Path, monkeypatch) -> None:
    """Phase 1 gate: the daytime mode still teaches the board, so long as traces accumulated."""
    from brain.reconciler import Reconciler

    cfg = _settings(tmp_path)
    cfg.nightly_reflect_hour = 0  # always due
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    db.set_mode("ASSIST")
    for index in range(5):
        db.upsert_trace_run(_trace_row(f"{index}" * 32, surface="builder_harness", outcome="ok"))
    assert db.count_unreflected_traces() >= 5
    rec = Reconciler(db, cfg, hub=TelemetryHub())
    calls: list[str] = []

    async def _record(*, run_key: str = "improver") -> None:
        calls.append(run_key)

    monkeypatch.setattr(rec, "run_skill_improver", _record)
    await rec.tick()
    assert calls, "nightly reflection did not run while the board was in ASSIST"
    assert db.get_state().mode == "ASSIST"


def test_prune_deletes_old_day_folders(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    old = cfg.traces_dir / "2020-01-01"
    old.mkdir(parents=True)
    (old / "trace-x.json").write_text("{}", encoding="utf-8")
    keep = cfg.traces_dir / "2999-01-01"
    keep.mkdir(parents=True)
    (keep / "trace-y.json").write_text("{}", encoding="utf-8")
    db.upsert_trace_run(_trace_row("c" * 32, created_at="2020-01-01T00:00:00Z"))
    removed = TraceSink(db, cfg, TelemetryHub()).prune()
    assert removed == 1
    assert not old.exists() and keep.exists()
    assert db.get_trace_run("c" * 32) is None


async def test_trace_sink_redacts_dotenv_secrets_and_confines_traversal_ids(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("LAYA_API_KEY", raising=False)
    cfg = _settings(tmp_path)
    cfg.home_dir.mkdir(parents=True, exist_ok=True)
    (cfg.home_dir / ".env").write_text("LAYA_API_KEY=laya_dotenv_secret_987654\n", encoding="utf-8")
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    hub = TelemetryHub()
    sink = TraceSink(db, cfg, hub)
    sink.start()
    try:
        hub.emit("voice.stt", turn_id="t9", trace_id="x/../../../../outside", text="leak laya_dotenv_secret_987654")
        hub.emit("voice.tts", turn_id="t9", trace_id="x/../../../../outside", text="echo laya_dotenv_secret_987654")
        assert await _settle(lambda: len(db.list_trace_runs(limit=5)) == 1)
    finally:
        await sink.stop()
        db.close()

    assert not (tmp_path / "outside.json").exists()
    for path in cfg.traces_dir.rglob("*"):
        if path.is_file():
            assert "laya_dotenv_secret_987654" not in path.read_text(encoding="utf-8", errors="replace")
