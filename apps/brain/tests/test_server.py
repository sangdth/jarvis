from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from brain_fakes import PlannerOrchestrator

from brain.config import Settings
from brain.db import BrainDB
from brain.reconciler import Reconciler
from brain.server import BrainServer


def _make_fake_pi(tmp_path: Path) -> Path:
    script = tmp_path / "fake_pi_server.py"
    script.write_text(
        """#!/usr/bin/env python3
import json, os, socket, sys
if "--session" in sys.argv:
    idx = sys.argv.index("--session")
    assert sys.argv[idx + 1] == "sess-plan-1", f"Unexpected session arg: {sys.argv[idx + 1]}"
sock_path = os.environ["BRAIN_RPC_SOCK"]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sock_path)
req = {
    "method": "upsert_task",
    "params": {
        "kind": "goal",
        "repo": "jarvis",
        "title": "Add test coverage",
        "spec": "Cover edge cases",
        "metric_target": "coverage >= 90%",
        "verify_cmd": "pytest",
        "ready": True,
    },
}
s.sendall((json.dumps(req) + "\\n").encode())
resp = json.loads(s.recv(4096).decode().splitlines()[0])
s.close()
print(json.dumps({"type": "session", "id": "sess-plan-1"}), flush=True)
print(json.dumps({
    "type": "message_end",
    "message": {"role": "assistant", "content": f"Queued task {resp['result']['id']}."},
    "cost_usd": 0.02,
}), flush=True)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


async def test_hermes_endpoints_auth_mode_brief_and_iphone_ask(tmp_path: Path) -> None:
    fake_pi = _make_fake_pi(tmp_path)
    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=tmp_path / "Projects",
        allowed_repos="jarvis",
        brain_api_key="secret-token",
        pi_bin=str(fake_pi),
        sandbox_enabled=False,
    )
    (cfg.projects_dir / "jarvis" / ".git").mkdir(parents=True, exist_ok=True)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg)
    brain_server = BrainServer(db, reconciler, cfg, orchestrator=PlannerOrchestrator(db, reconciler))

    async with TestClient(TestServer(brain_server.app)) as client:
        # 1. Unauthorized without Bearer token
        unauth = await client.post("/v1/ask", json={"input": "morning brief"})
        assert unauth.status == 401

        headers = {"Authorization": "Bearer secret-token"}

        # 2. iPhone /v1/ask switches to autonomous mode
        ask_resp = await client.post(
            "/v1/ask", json={"input": "Switch to autonomous mode"}, headers=headers
        )
        assert ask_resp.status == 200
        ask_data = await ask_resp.json()
        assert "autonomous mode" in ask_data["spoken"].lower()
        assert db.get_state().mode == "AUTONOMOUS"

        # 3. HermesAdapter POST /v1/runs + GET /v1/runs/{id}/events SSE stream
        run_resp = await client.post(
            "/v1/runs",
            json={"input": "Create a goal to add test coverage in jarvis"},
            headers=headers,
        )
        assert run_resp.status == 200
        run_id = (await run_resp.json())["run_id"]

        events: list[dict[str, object]] = []
        async with client.get(f"/v1/runs/{run_id}/events", headers=headers) as sse:
            assert sse.status == 200
            async for raw_line in sse.content:
                line = raw_line.decode("utf-8").strip()
                if line.startswith("data:"):
                    ev = json.loads(line.removeprefix("data:").strip())
                    events.append(ev)
                    if ev.get("event") in ("run.completed", "run.failed", "run.cancelled"):
                        break

        assert any(e.get("event") == "run.completed" for e in events)
        tasks = db.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].id == "SG-1"

        # 4. Drop-recovery endpoint GET /v1/runs/{id}
        status_resp = await client.get(f"/v1/runs/{run_id}", headers=headers)
        assert status_resp.status == 200
        status_json = await status_resp.json()
        assert status_json["status"] == "completed"
        assert "SG-1" in status_json["output"]

        # 4b. Second interactive turn in same HermesAdapter session reuses mapped pi session ID
        run2_resp = await client.post(
            "/v1/runs",
            json={"input": "Add another goal in jarvis", "session_id": run_id},
            headers=headers,
        )
        run2_id = (await run2_resp.json())["run_id"]
        async with client.get(f"/v1/runs/{run2_id}/events", headers=headers) as sse:
            async for raw_line in sse.content:
                line = raw_line.decode("utf-8").strip()
                if line.startswith("data:"):
                    ev = json.loads(line.removeprefix("data:").strip())
                    if ev.get("event") in ("run.completed", "run.failed"):
                        assert ev.get("event") == "run.completed"
                        break
        assert abs(db.get_state().spent_today_usd - 0.04) < 1e-6

        # 5. Morning brief with queued outbox approval + POST /v1/runs/{id}/approval
        ob = db.queue_outbox(
            kind="approval_request",
            task_id="SG-1",
            summary_spoken="Approve running database migration?",
        )
        brief_run = await client.post(
            "/v1/runs", json={"input": "morning brief"}, headers=headers
        )
        brief_run_id = (await brief_run.json())["run_id"]

        async with client.get(f"/v1/runs/{brief_run_id}/events", headers=headers) as sse:
            first_ev = None
            async for raw_line in sse.content:
                line = raw_line.decode("utf-8").strip()
                if line.startswith("data:"):
                    first_ev = json.loads(line.removeprefix("data:").strip())
                    break
            assert first_ev is not None
            assert first_ev["event"] == "approval.request"
            assert first_ev["request_id"] == ob.id

            # Answer approval via HermesAdapter endpoint
            app_resp = await client.post(
                f"/v1/runs/{brief_run_id}/approval",
                json={"choice": "once", "request_id": ob.id},
                headers=headers,
            )
            assert app_resp.status == 200

            remaining: list[dict[str, object]] = []
            async for raw_line in sse.content:
                line = raw_line.decode("utf-8").strip()
                if line.startswith("data:"):
                    ev = json.loads(line.removeprefix("data:").strip())
                    remaining.append(ev)
                    if ev.get("event") == "run.completed":
                        break
            assert any(e.get("event") == "approval.responded" for e in remaining)
            assert any(e.get("event") == "run.completed" for e in remaining)

        # 6. Malformed JSON returns 400 (never 500), and `/stop` on completed run preserves status
        bad_json = await client.post(
            "/v1/runs",
            data="{not-json",
            headers={**headers, "Content-Type": "application/json"},
        )
        assert bad_json.status == 400

        non_dict = await client.post("/v1/ask", json=["not", "a", "dict"], headers=headers)
        assert non_dict.status == 400

        stop_completed = await client.post(f"/v1/runs/{run_id}/stop", headers=headers)
        assert stop_completed.status == 200
        after_stop = await (await client.get(f"/v1/runs/{run_id}", headers=headers)).json()
        assert after_stop["status"] == "completed"

    await reconciler.stop()
    db.close()


async def test_empty_api_key_fails_closed_and_live_approval_stop_and_deferred_planner_resume(
    tmp_path: Path,
) -> None:
    fake_pi = tmp_path / "fake_pi_live.py"
    fake_pi.write_text(
        r"""#!/usr/bin/env python3
import json, os, pathlib, socket, sys, time

# Verify Planner is also inside Seatbelt sandbox (cannot read state.db-wal or .env)
wal_file = os.environ.get("TEST_WAL_PATH")
if wal_file:
    try:
        pathlib.Path(wal_file).read_text(encoding="utf-8")
        raise SystemExit("SANDBOX_FAILED_TO_BLOCK_WAL")
    except OSError:
        pass

prompt = sys.argv[-1]
sock_path = os.environ["BRAIN_RPC_SOCK"]

if "SLOW_CANCEL_TEST" in prompt:
    time.sleep(30)
    sys.exit(0)

if "--session" in sys.argv:
    print(json.dumps({"type": "session", "id": "sess-deferred-1"}), flush=True)
    print(json.dumps({
        "type": "message_end",
        "message": {"role": "assistant", "content": "Resumed deferred planner action."},
        "cost_usd": 0.01,
    }), flush=True)
    sys.exit(0)

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sock_path)
s.sendall((json.dumps({
    "method": "defer_for_approval",
    "params": {"summary_spoken": "Approve external action?", "session_id": "sess-deferred-1"},
}) + "\n").encode())
resp = json.loads(s.recv(4096).decode().splitlines()[0])
s.close()
approved = resp["result"].get("approved")
deferred = resp["result"].get("deferred")
print(json.dumps({"type": "session", "id": "sess-deferred-1"}), flush=True)
print(json.dumps({
    "type": "message_end",
    "message": {"role": "assistant", "content": f"LiveResult approved={approved} deferred={deferred}"},
    "cost_usd": 0.01,
}), flush=True)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(fake_pi.stat().st_mode | stat.S_IEXEC)

    # 1. Empty brain_api_key fails closed with 401
    empty_cfg = Settings(
        home_dir=tmp_path / "empty_home",
        projects_dir=tmp_path / "Projects",
        brain_api_key="",
        pi_bin=str(fake_pi),
    )
    empty_db = BrainDB(empty_cfg.db_path, empty_cfg.allowed_repo_set)
    empty_rec = Reconciler(empty_db, empty_cfg)
    empty_srv = BrainServer(empty_db, empty_rec, empty_cfg, orchestrator=PlannerOrchestrator(empty_db, empty_rec))
    async with TestClient(TestServer(empty_srv.app)) as unauth_client:
        r = await unauth_client.post("/v1/ask", json={"input": "morning brief"})
        assert r.status == 401
    await empty_rec.stop()
    empty_db.close()

    # 2. Live mid-run approval (`_make_approval_cb`), `/stop`, and iPhone `task_id=None` deferred resume
    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=tmp_path / "Projects",
        allowed_repos="jarvis",
        brain_api_key="secret",
        pi_bin=str(fake_pi),
        sandbox_enabled=True,
    )
    (cfg.projects_dir / "jarvis" / ".git").mkdir(parents=True, exist_ok=True)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set, default_mode="ASSIST")
    wal_path = cfg.home_dir / "state.db-wal"
    wal_path.write_text("SEALED_WAL_CONTENT", encoding="utf-8")
    import os

    os.environ["TEST_WAL_PATH"] = str(wal_path)
    reconciler = Reconciler(db, cfg)
    srv = BrainServer(db, reconciler, cfg, orchestrator=PlannerOrchestrator(db, reconciler))
    headers = {"Authorization": "Bearer secret"}

    try:
        async with TestClient(TestServer(srv.app)) as client:
            # 2a. Live SSE approval during an active `/v1/runs` Planner turn
            live_resp = await client.post(
                "/v1/runs", json={"input": "Need live approval"}, headers=headers
            )
            live_id = (await live_resp.json())["run_id"]
            async with client.get(f"/v1/runs/{live_id}/events", headers=headers) as sse:
                ev1 = None
                async for raw in sse.content:
                    line = raw.decode().strip()
                    if line.startswith("data:"):
                        ev1 = json.loads(line.removeprefix("data:").strip())
                        break
                assert ev1 is not None and ev1["event"] == "approval.request"
                ans = await client.post(
                    f"/v1/runs/{live_id}/approval",
                    json={"choice": "once", "request_id": ev1["request_id"]},
                    headers=headers,
                )
                assert ans.status == 200
                final_ev = None
                async for raw in sse.content:
                    line = raw.decode().strip()
                    if line.startswith("data:"):
                        ev = json.loads(line.removeprefix("data:").strip())
                        if ev.get("event") in ("run.completed", "run.failed"):
                            final_ev = ev
                            break
                assert final_ev is not None and final_ev["event"] == "run.completed"
                assert "approved=True" in final_ev["output"]

            # 2b. iPhone `/v1/ask` (`allow_live_approval=False`) defers with `task_id=None`,
            # and approving in `morning brief` resumes `sess-deferred-1`!
            ask_def = await client.post(
                "/v1/ask", json={"input": "Defer from iPhone"}, headers=headers
            )
            assert ask_def.status == 200
            brief = await client.post(
                "/v1/runs", json={"input": "morning brief"}, headers=headers
            )
            brief_id = (await brief.json())["run_id"]
            async with client.get(f"/v1/runs/{brief_id}/events", headers=headers) as sse:
                req_ev = None
                async for raw in sse.content:
                    line = raw.decode().strip()
                    if line.startswith("data:"):
                        req_ev = json.loads(line.removeprefix("data:").strip())
                        break
                assert req_ev is not None and req_ev["event"] == "approval.request"
                await client.post(
                    f"/v1/runs/{brief_id}/approval",
                    json={"choice": "once", "request_id": req_ev["request_id"]},
                    headers=headers,
                )
                done_ev = None
                async for raw in sse.content:
                    line = raw.decode().strip()
                    if line.startswith("data:"):
                        ev = json.loads(line.removeprefix("data:").strip())
                        if ev.get("event") == "run.completed":
                            done_ev = ev
                            break
                assert done_ev is not None
                assert "Resumed deferred planner action." in done_ev["output"]

            # 2c. Active `/v1/runs/{id}/stop` cancels a slow run
            slow_resp = await client.post(
                "/v1/runs", json={"input": "SLOW_CANCEL_TEST"}, headers=headers
            )
            slow_id = (await slow_resp.json())["run_id"]
            stop_resp = await client.post(f"/v1/runs/{slow_id}/stop", headers=headers)
            assert stop_resp.status == 200
            st = await (await client.get(f"/v1/runs/{slow_id}", headers=headers)).json()
            assert st["status"] == "cancelled"
    finally:
        os.environ.pop("TEST_WAL_PATH", None)
        await reconciler.stop()
        db.close()


async def test_the_gateway_probe_status_board_tasks_and_restart(tmp_path: Path) -> None:
    """One HTTP surface: an unauthenticated liveness probe, a query-string token, and the operator routes."""
    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=tmp_path / "Projects",
        allowed_repos="jarvis",
        brain_api_key="secret-token",
        sandbox_enabled=False,
    )
    (cfg.projects_dir / "jarvis" / ".git").mkdir(parents=True, exist_ok=True)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    restarts: list[int] = []
    reconciler = Reconciler(db, cfg, on_restart_requested=lambda: restarts.append(1))
    server = BrainServer(db, reconciler, cfg, voice_snapshot=lambda: {"state": "listening", "remote": True})

    async with TestClient(TestServer(server.app)) as client:
        # 1. `/health` needs no secret: a phone or a load balancer can ask if Jarvis is up.
        health = await client.get("/health")
        assert health.status == 200
        assert (await health.json())["service"] == "jarvis"

        # 2. Everything else is guarded, and the token may ride in the query string (browser, iOS Shortcut).
        assert (await client.get("/v1/status")).status == 401
        assert (await client.get("/v1/status?token=nope")).status == 401
        status = await (await client.get("/v1/status?token=secret-token")).json()
        assert status["ok"] is True
        assert status["voice"]["state"] == "listening"
        assert status["mode"] == "ASSIST"

        headers = {"Authorization": "Bearer secret-token"}

        # 3. Queue a task over HTTP, exactly as `jarvis add` does.
        created = await client.post(
            "/v1/tasks",
            json={"kind": "problem", "repo": "jarvis", "title": "Flaky test", "spec": "Fix it", "ready": True},
            headers=headers,
        )
        assert created.status == 200
        task = (await created.json())["task"]
        assert task["id"] == "PRB-1"

        listed = await (await client.get("/v1/tasks", headers=headers)).json()
        assert [t["id"] for t in listed["tasks"]] == ["PRB-1"]

        # 4. The full board in one body, so a remote cockpit needs no SQLite access.
        board = await (await client.get("/v1/board", headers=headers)).json()
        assert board["tasks"][0]["id"] == "PRB-1"
        assert "state" in board and "attempts" in board and "outbox" in board

        # 5. Mode switching, and a refused value.
        mode = await client.post("/v1/mode", json={"mode": "autonomous"}, headers=headers)
        assert (await mode.json())["mode"] == "AUTONOMOUS"
        assert (await client.post("/v1/mode", json={"mode": "turbo"}, headers=headers)).status == 400

        # 6. Restart goes through the supervisor hook, not a raw signal.
        restarted = await client.post("/v1/restart", headers=headers)
        assert (await restarted.json())["restarting"] is True
        await asyncio.sleep(0.7)  # the handler schedules it half a second out
        assert restarts == [1]

    await reconciler.stop()
    db.close()


async def test_pairing_hands_each_device_its_own_revocable_secret(tmp_path: Path) -> None:
    """`jarvis pair`: a one-time code becomes a per-device secret; revoking it leaves everyone else alone."""
    from jarvis_core.devices import DeviceStore

    store = DeviceStore(tmp_path / "devices.json")
    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=tmp_path / "Projects",
        allowed_repos="jarvis",
        brain_api_key="secret-token",
        sandbox_enabled=False,
    )
    (cfg.projects_dir / "jarvis" / ".git").mkdir(parents=True, exist_ok=True)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg)
    server = BrainServer(db, reconciler, cfg, devices=store)
    owner = {"Authorization": "Bearer secret-token"}

    async with TestClient(TestServer(server.app)) as client:
        # 1. Minting a code is the owner's alone; a device secret cannot do it.
        assert (await client.post("/v1/pair/start")).status == 401
        started = await client.post("/v1/pair/start", headers=owner)
        assert started.status == 200
        code = (await started.json())["code"]
        assert len(code) == 6 and code.isdigit()

        # 2. A wrong code is refused and counted.
        assert (await client.post("/v1/pair", json={"code": "000000", "name": "x"})).status == 403

        # 3. The right code pairs once and yields a secret.
        paired = await client.post("/v1/pair", json={"code": code, "name": "Sang's MacBook"})
        assert paired.status == 200
        payload = await paired.json()
        secret, device_id = payload["secret"], payload["device"]["id"]
        assert payload["device"]["name"] == "Sang's MacBook"
        assert "secret_sha256" not in payload["device"]

        # 4. The code is spent: a replay cannot mint a second device.
        assert (await client.post("/v1/pair", json={"code": code, "name": "x"})).status == 403

        # 5. The device secret works on the API but not on device administration.
        device_auth = {"Authorization": f"Bearer {secret}"}
        assert (await client.get("/v1/status", headers=device_auth)).status == 200
        assert (await client.get("/v1/devices", headers=device_auth)).status == 401
        assert (await client.post("/v1/pair/start", headers=device_auth)).status == 401

        # 6. The owner sees it, revokes it, and only that secret dies.
        listed = await (await client.get("/v1/devices", headers=owner)).json()
        assert [d["id"] for d in listed["devices"]] == [device_id]
        assert (await client.post(f"/v1/devices/{device_id}/revoke", headers=owner)).status == 200
        assert (await client.get("/v1/status", headers=device_auth)).status == 401
        assert (await client.get("/v1/status", headers=owner)).status == 200
        assert (await client.post("/v1/devices/dev-nope/revoke", headers=owner)).status == 404

    await reconciler.stop()
    db.close()


async def test_pairing_stops_the_guessing(tmp_path: Path) -> None:
    """Six wrong codes in a row: the pending code is dropped and the attempt is rate limited."""
    from jarvis_core.devices import DeviceStore

    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=tmp_path / "Projects",
        allowed_repos="jarvis",
        brain_api_key="secret-token",
        sandbox_enabled=False,
    )
    (cfg.projects_dir / "jarvis" / ".git").mkdir(parents=True, exist_ok=True)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg)
    server = BrainServer(db, reconciler, cfg, devices=DeviceStore(tmp_path / "d.json"))
    owner = {"Authorization": "Bearer secret-token"}

    async with TestClient(TestServer(server.app)) as client:
        code = (await (await client.post("/v1/pair/start", headers=owner)).json())["code"]
        for _ in range(5):
            assert (await client.post("/v1/pair", json={"code": "000000"})).status == 403
        assert (await client.post("/v1/pair", json={"code": "000000"})).status == 429
        # The real code no longer works either: a fresh one has to be minted.
        assert (await client.post("/v1/pair", json={"code": code})).status == 429

    await reconciler.stop()
    db.close()


async def test_the_bootstrap_routes_bypass_the_shared_token(tmp_path: Path) -> None:
    """`/client.py` (the pairing script) and `/ws` (the relay) answer before any credential: they check their own."""
    from aiohttp import web

    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=tmp_path / "Projects",
        allowed_repos="jarvis",
        brain_api_key="secret-token",
        sandbox_enabled=False,
    )
    (cfg.projects_dir / "jarvis" / ".git").mkdir(parents=True, exist_ok=True)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg)

    async def ok(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    server = BrainServer(
        db,
        reconciler,
        cfg,
        extra_routes=[web.get("/client.py", ok), web.get("/ws", ok)],
    )
    async with TestClient(TestServer(server.app)) as client:
        assert (await client.get("/client.py")).status == 200  # no token: the laptop has not paired yet
        assert (await client.get("/ws")).status == 200  # the relay's own handler rejects a bad credential
        assert (await client.get("/v1/status")).status == 401  # everything else still needs one

    await reconciler.stop()
    db.close()
