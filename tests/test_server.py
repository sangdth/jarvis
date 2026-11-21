from __future__ import annotations

import json
import stat
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from superbrain.config import Settings
from superbrain.db import BrainDB
from superbrain.reconciler import Reconciler
from superbrain.server import BrainServer


def _make_fake_pi(tmp_path: Path) -> Path:
    script = tmp_path / "fake_pi_server.py"
    script.write_text(
        """#!/usr/bin/env python3
import json, os, socket, sys
if "--session" in sys.argv:
    idx = sys.argv.index("--session")
    assert sys.argv[idx + 1] == "sess-plan-1", f"Unexpected session arg: {sys.argv[idx + 1]}"
sock_path = os.environ["SUPERBRAIN_RPC_SOCK"]
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
        superbrain_api_key="secret-token",
        pi_bin=str(fake_pi),
        sandbox_enabled=False,
    )
    (cfg.projects_dir / "jarvis" / ".git").mkdir(parents=True, exist_ok=True)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg)
    brain_server = BrainServer(db, reconciler, cfg)

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

    await reconciler.stop()
    db.close()
