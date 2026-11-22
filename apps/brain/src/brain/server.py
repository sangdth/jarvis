from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from aiohttp import web
from loguru import logger

from brain.config import Settings, settings
from brain.db import BrainDB
from brain.reconciler import Reconciler
from brain.telemetry import (
    EVENT_APPROVAL_REQUEST,
    EVENT_APPROVAL_RESPONDED,
    EVENT_HTTP_COMPLETED,
    EVENT_HTTP_INBOUND,
    EVENT_RUN_STATUS,
    TelemetryHub,
    preview,
)
from jarvis_core.devices import DeviceStore
from jarvis_core.home import JARVIS_HOME, REPO_ROOT, git_version
from jarvis_core.net import advertised_hosts

# A pairing code is short, single-use and short-lived; five wrong guesses in a minute stops the guessing.
PAIR_TTL_SECS = 300
PAIR_MAX_FAILURES = 5
PAIR_FAILURE_WINDOW_SECS = 60.0

_AUTONOMOUS_RE = re.compile(
    r"\b(switch\s+to\s+autonomous|start\s+(?:the\s+)?night\s+(?:mode|run)|autonomous\s+mode)\b",
    re.IGNORECASE,
)
_ASSIST_RE = re.compile(
    r"\b(switch\s+to\s+assist|pause\s+background|hold\s+background|assist\s+mode|day\s+mode)\b",
    re.IGNORECASE,
)
_BRIEF_RE = re.compile(
    r"\b(morning\s+brief|what\s+happened\s+overnight|brain\s+status|status\s+report)\b",
    re.IGNORECASE,
)
_IMPROVE_RE = re.compile(
    r"\b(improve\s+skills|learn\s+from\s+recent\s+runs|reflect\s+on\s+recent\s+runs|self[\s-]+improve)\b",
    re.IGNORECASE,
)
_RUN_TASK_RE = re.compile(r"\brun\s+((?:SG|PRB)-\d+(?:\.\d+)?)\b", re.IGNORECASE)


@dataclass
class RunRecord:
    run_id: str
    session_id: str
    status: str = "running"  # 'running' | 'completed' | 'failed' | 'cancelled'
    output: str = ""
    error: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    pending_approval_future: asyncio.Future[bool] | None = None
    pending_outbox_id: str | None = None
    task_handle: asyncio.Task[None] | None = None

    def publish(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        for q in list(self.subscribers):
            q.put_nowait(event)


class BrainServer:
    """
    The one Jarvis network surface: the brain's HTTP/SSE contract, the thin client's mic and speaker routes, the
    cockpit's telemetry, and the small operator API a probe or a script needs (`/health`, `/v1/status`, `/v1/board`,
    `/v1/tasks`, `/v1/mode`, `/v1/restart`). Everything except `/health` needs the shared token.
    """

    def __init__(
        self,
        db: BrainDB,
        reconciler: Reconciler,
        cfg: Settings = settings,
        hub: TelemetryHub | None = None,
        *,
        devices: DeviceStore | None = None,
        voice_snapshot: Callable[[], dict[str, Any]] | None = None,
        extra_routes: list[web.RouteDef] | None = None,
        started_at: float | None = None,
    ) -> None:
        self.db = db
        self.reconciler = reconciler
        self.cfg = cfg
        self.hub = hub if hub is not None else reconciler.hub
        self.runs: dict[str, RunRecord] = {}
        self._pi_sessions: dict[str, str] = {}
        self.devices = devices if devices is not None else DeviceStore.load()
        # One pairing code at a time, in memory: a restart clears it, which is what we want.
        self._pair_codes: dict[str, float] = {}
        self._pair_failures: deque[float] = deque(maxlen=PAIR_MAX_FAILURES * 2)
        self._voice_snapshot = voice_snapshot or (lambda: {"state": "not running"})
        self._started_at = started_at if started_at is not None else time.time()
        self.app = web.Application(middlewares=[self._auth_middleware])
        self.app.add_routes(
            [
                web.get("/health", self.handle_health),
                web.post("/v1/pair/start", self.handle_pair_start),
                web.post("/v1/pair", self.handle_pair),
                web.get("/v1/devices", self.handle_devices),
                web.post("/v1/devices/{device_id}/revoke", self.handle_revoke),
                web.get("/v1/status", self.handle_status),
                web.get("/v1/board", self.handle_board),
                web.get("/v1/tasks", self.handle_list_tasks),
                web.post("/v1/tasks", self.handle_add_task),
                web.post("/v1/tasks/{task_id}/research", self.handle_research),
                web.post("/v1/mode", self.handle_mode),
                web.post("/v1/skills/improve", self.handle_improve),
                web.post("/v1/restart", self.handle_restart),
                web.post("/v1/runs", self.handle_create_run),
                web.get("/v1/runs/{run_id}", self.handle_get_run),
                web.get("/v1/runs/{run_id}/events", self.handle_stream_events),
                web.post("/v1/runs/{run_id}/approval", self.handle_approval),
                web.post("/v1/runs/{run_id}/stop", self.handle_stop_run),
                web.post("/v1/ask", self.handle_ask),
                web.get("/v1/telemetry/events", self.handle_telemetry_events),
                web.get("/v1/telemetry/snapshot", self.handle_telemetry_snapshot),
                web.post("/v1/outbox/{outbox_id}/resolve", self.handle_outbox_resolve),
                *(extra_routes or []),
            ]
        )

    @staticmethod
    def request_token(request: web.Request) -> str:
        """The shared token, from `Authorization: Bearer`, `?token=`, or `X-Jarvis-Token` (all clients work)."""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth.removeprefix("Bearer ").strip()
        return (request.query.get("token") or request.headers.get("X-Jarvis-Token") or "").strip()

    @web.middleware
    async def _auth_middleware(
        self,
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        if request.path == "/health":
            return await handler(request)  # the liveness probe: no secret, and no state
        if request.method == "POST" and request.path == "/v1/pair":
            return await handler(request)  # the bootstrap: the pairing code is the credential
        if request.path == "/client.py":
            return await handler(request)  # the script that pairs: it carries no secret, only this server's address
        if request.path == "/ws":
            return await handler(request)  # the relay checks its own credential, shared token or device secret
        token = self.request_token(request)
        if self._is_owner(token):
            return await handler(request)
        if request.path.startswith(("/v1/pair/", "/v1/devices")):
            # Minting and revoking devices is the owner's business; a paired device cannot do it.
            return web.json_response({"error": "owner token required"}, status=401)
        if self.devices.verify(token) is not None:
            return await handler(request)
        return web.json_response({"error": "Unauthorized"}, status=401)

    def _is_owner(self, token: str) -> bool:
        expected = self.cfg.brain_api_key.strip()
        return bool(expected) and hmac.compare_digest(token, expected)

    @staticmethod
    async def _read_json_dict(request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "Invalid JSON body"}),
                content_type="application/json",
            ) from exc
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "Expected JSON object"}),
                content_type="application/json",
            )
        return body

    def _prune_history(self, max_entries: int = 200) -> None:
        if len(self.runs) > max_entries:
            finished = [
                rid
                for rid, r in self.runs.items()
                if r.status in ("completed", "failed", "cancelled")
            ]
            for rid in finished[: max(0, len(self.runs) - max_entries)]:
                self.runs.pop(rid, None)
        while len(self._pi_sessions) > max_entries:
            oldest_key = next(iter(self._pi_sessions))
            self._pi_sessions.pop(oldest_key, None)

    @staticmethod
    def async_json(data: Any) -> web.Response:
        return web.json_response(data)

    def _task_dict(self, t: Any) -> dict[str, Any]:
        return {
            "id": t.id,
            "parent_id": t.parent_id,
            "kind": t.kind,
            "mode": t.mode,
            "repo": t.repo,
            "title": t.title,
            "spec": t.spec,
            "metric_target": t.metric_target,
            "verify_cmd": t.verify_cmd,
            "status": t.status,
            "priority": t.priority,
            "strike_count": t.strike_count,
            "child_strategy": t.child_strategy,
            "best_metric": t.best_metric,
            "best_branch": t.best_branch,
            "best_sha": t.best_sha,
            "lease_until": t.lease_until,
            "created_at": t.created_at,
            "updated_at": t.updated_at,
        }

    def _attempt_dict(self, a: Any) -> dict[str, Any]:
        return {
            "id": a.id,
            "task_id": a.task_id,
            "role": a.role,
            "pi_session_id": a.pi_session_id,
            "session_jsonl_path": a.session_jsonl_path,
            "summary": a.summary,
            "git_branch": a.git_branch,
            "git_sha": a.git_sha,
            "cost_usd": a.cost_usd,
            "outcome": a.outcome,
            "verify_output": a.verify_output,
            "metric_value": a.metric_value,
            "skills_used": list(a.skills_used),
            "diagnostic": a.diagnostic,
            "reflected": a.reflected,
            "created_at": a.created_at,
        }

    def _skill_dict(self, s: Any) -> dict[str, Any]:
        return {
            "slug": s.slug,
            "name": s.name,
            "description": s.description,
            "version": s.version,
            "sha256": s.sha256,
            "status": s.status,
            "content": s.content,
            "lesson": s.lesson,
            "evidence_attempt_ids": list(s.evidence_attempt_ids),
            "loads_since_update": s.loads_since_update,
            "successes_since_update": s.successes_since_update,
            "failures_since_update": s.failures_since_update,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }

    def _outbox_dict(self, o: Any) -> dict[str, Any]:
        try:
            details = json.loads(o.details_json or "{}")
        except ValueError:
            details = {}
        return {
            "id": o.id,
            "kind": o.kind,
            "task_id": o.task_id,
            "resume_session_id": o.resume_session_id,
            "summary_spoken": o.summary_spoken,
            "details": details,
            "status": o.status,
            "created_at": o.created_at,
        }

    def status_payload(self) -> dict[str, Any]:
        """Everything a probe, `jarvis status`, or a phone needs: version, uptime, mode, spend, counts, voice."""
        st = self.db.get_state()
        tasks = self.db.list_tasks()
        skills = self.db.list_skills(status="active")
        outbox = self.db.list_outbox()
        return {
            "ok": True,
            "version": git_version(),
            "repo": str(REPO_ROOT),
            "home": str(JARVIS_HOME),
            "pid": os.getpid(),
            "uptime_s": round(time.time() - self._started_at, 1),
            "mode": st.mode,
            "spent_today_usd": round(st.spent_today_usd, 4),
            "daily_budget_usd": st.daily_budget_usd,
            "budget_remaining_usd": round(st.budget_remaining_usd, 4),
            "tasks": len(tasks),
            "tasks_ready": sum(1 for t in tasks if t.status == "ready"),
            "tasks_running": sum(1 for t in tasks if t.status == "in_progress"),
            "skills_active": len(skills),
            "approvals_waiting": sum(1 for o in outbox if o.kind == "approval_request" and o.status == "queued"),
            "telemetry_seq": self.hub.sequence,
            "voice": self._voice_snapshot(),
        }

    async def handle_health(self, request: web.Request) -> web.Response:
        """Unauthenticated liveness probe: is a Jarvis answering here, and which build?"""
        return web.json_response(
            {
                "ok": True,
                "service": "jarvis",
                "version": git_version(),
                "pid": os.getpid(),
                "uptime_s": round(time.time() - self._started_at, 1),
            }
        )

    async def handle_status(self, request: web.Request) -> web.Response:
        return web.json_response(self.status_payload())

    def authenticate(self, token: str) -> bool:
        """The shared token, or a paired device's secret. Used by the relay for `/ws` and `/client.py`."""
        return self._is_owner(token) or self.devices.verify(token) is not None

    async def handle_pair_start(self, request: web.Request) -> web.Response:
        """`jarvis pair`: mint a short code. One at a time, single use, five minutes."""
        code = f"{secrets.randbelow(1_000_000):06d}"
        self._pair_codes = {code: time.time() + PAIR_TTL_SECS}
        self._pair_failures.clear()
        logger.info("[Pair] a pairing code was issued (single use, 5 minutes).")
        hosts = advertised_hosts()
        server = f"http://{hosts[0]}:{self.cfg.brain_port}" if hosts else self.cfg.endpoint_url
        return web.json_response({"code": code, "expires_in": PAIR_TTL_SECS, "server": server})

    async def handle_pair(self, request: web.Request) -> web.Response:
        """A device trades the code for its own secret; the code is consumed by the attempt."""
        body = await self._read_json_dict(request)
        code = "".join(str(body.get("code") or "").split())
        now = time.time()
        recent = [t for t in self._pair_failures if now - t < PAIR_FAILURE_WINDOW_SECS]
        self._pair_failures.clear()
        self._pair_failures.extend(recent)
        if len(recent) >= PAIR_MAX_FAILURES:
            self._pair_codes.clear()  # stop the guessing: a new code has to be minted
            logger.warning("[Pair] too many wrong codes; pending codes cleared.")
            return web.json_response({"error": "too many attempts; start pairing again"}, status=429)
        self._pair_codes = {c: expires for c, expires in self._pair_codes.items() if expires > now}
        match = next((known for known in self._pair_codes if hmac.compare_digest(known, code)), None)
        if match is None:
            self._pair_failures.append(now)
            logger.warning("[Pair] a wrong or expired code was tried.")
            return web.json_response({"error": "wrong or expired code"}, status=403)
        self._pair_codes.pop(match, None)  # single use
        device, secret = self.devices.add(str(body.get("name") or ""))
        logger.info(f"[Pair] paired {device.name!r} ({device.id}); its secret is shown once and kept by that device.")
        self.hub.emit(EVENT_HTTP_INBOUND, source="pair", route="/v1/pair", device_id=device.id, name=device.name)
        return web.json_response({"device": device.public(), "secret": secret})

    async def handle_devices(self, request: web.Request) -> web.Response:
        return web.json_response({"devices": [d.public() for d in self.devices.devices()]})

    async def handle_revoke(self, request: web.Request) -> web.Response:
        device = self.devices.revoke(request.match_info["device_id"])
        if device is None:
            return web.json_response({"error": "unknown device"}, status=404)
        logger.info(f"[Pair] revoked {device.name!r} ({device.id}).")
        return web.json_response({"device": device.public()})

    async def handle_board(self, request: web.Request) -> web.Response:
        """The full board snapshot in one body: what a remote cockpit draws, with no direct SQLite access."""
        st = self.db.get_state()
        return web.json_response(
            {
                "state": {
                    "mode": st.mode,
                    "daily_budget_usd": st.daily_budget_usd,
                    "spent_today_usd": st.spent_today_usd,
                    "spend_date": st.spend_date,
                    "last_good_sha": st.last_good_sha,
                    "upgrade_runs": st.upgrade_runs,
                    "upgrade_failures": st.upgrade_failures,
                },
                "tasks": [self._task_dict(t) for t in self.db.list_tasks()],
                "attempts": [self._attempt_dict(a) for a in self.db.list_attempts()],
                "skills": [self._skill_dict(s) for s in self.db.list_skills(status=None)],
                "outbox": [self._outbox_dict(o) for o in self.db.list_outbox()],
            }
        )

    async def handle_list_tasks(self, request: web.Request) -> web.Response:
        raw = (request.query.get("status") or "").strip()
        valid = {"draft", "ready", "in_progress", "done", "blocked"}
        if raw and raw not in valid:
            return web.json_response({"error": f"status must be one of {sorted(valid)}"}, status=400)
        raw_mode = (request.query.get("mode") or "").strip().upper()
        if raw_mode and raw_mode not in ("ASSIST", "AUTONOMOUS"):
            return web.json_response({"error": "mode must be ASSIST or AUTONOMOUS"}, status=400)
        tasks = self.db.list_tasks(
            status=raw or None,  # type: ignore[arg-type]
            mode=raw_mode or None,  # type: ignore[arg-type]
        )
        return web.json_response({"tasks": [self._task_dict(t) for t in tasks]})

    async def handle_add_task(self, request: web.Request) -> web.Response:
        """Queue a goal or a problem from a script or a phone, the same call `jarvis add` makes."""
        body = await self._read_json_dict(request)
        kind = str(body.get("kind") or "goal").strip().lower()
        if kind not in ("goal", "problem"):
            return web.json_response({"error": "kind must be goal or problem"}, status=400)
        try:
            task = self.db.upsert_task(
                task_id=(str(body.get("id")).strip() or None) if body.get("id") else None,
                kind=kind,  # type: ignore[arg-type]
                mode=(str(body.get("mode")).strip() or None) if body.get("mode") else None,
                repo=str(body.get("repo") or "jarvis"),
                title=str(body.get("title") or "").strip(),
                spec=str(body.get("spec") or "").strip(),
                metric_target=body.get("metric"),
                verify_cmd=body.get("verify"),
                priority=int(body.get("priority") or 50),
                ready=True if body.get("ready") else None,
            )
        except (ValueError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"task": self._task_dict(task)})

    async def handle_research(self, request: web.Request) -> web.Response:
        task_id = request.match_info["task_id"]
        res = await self.reconciler.run_auto_research(task_id)
        return web.json_response(
            {
                "outcome": res.outcome,
                "summary": res.summary,
                "hypotheses": list(res.upserted_task_ids or []),
            }
        )

    async def handle_mode(self, request: web.Request) -> web.Response:
        body = await self._read_json_dict(request)
        mode = str(body.get("mode") or "").upper()
        if mode not in ("ASSIST", "AUTONOMOUS"):
            return web.json_response({"error": "mode must be ASSIST or AUTONOMOUS"}, status=400)
        st = self.db.set_mode(mode)  # type: ignore[arg-type]
        return web.json_response({"ok": True, "mode": st.mode, "budget_remaining_usd": round(st.budget_remaining_usd, 4)})

    async def handle_improve(self, request: web.Request) -> web.Response:
        res = await self.reconciler.run_skill_improver()
        return web.json_response(
            {"outcome": res.outcome, "summary": res.summary, "modified": list(res.modified_skills or [])}
        )

    async def handle_restart(self, request: web.Request) -> web.Response:
        """Restart the whole daemon (voice included) on the current checkout; launchd brings it back."""
        if self.reconciler.on_restart_requested is None:
            return web.json_response({"error": "no restart hook: this Jarvis is not supervised"}, status=409)
        self.reconciler.restart_pending = True
        self._schedule_restart_if_pending()
        return web.json_response({"ok": True, "restarting": True})

    async def handle_create_run(self, request: web.Request) -> web.Response:
        body = await self._read_json_dict(request)
        user_input = str(body.get("input") or "").strip()
        if not user_input:
            return web.json_response({"error": "input is required"}, status=400)

        self._prune_history()
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        session_id = str(body.get("session_id") or run_id)
        rec = RunRecord(run_id=run_id, session_id=session_id)
        self.runs[run_id] = rec
        self.hub.emit(
            EVENT_HTTP_INBOUND,
            source="macbook_hermes",
            route="/v1/runs",
            run_id=run_id,
            session_id=session_id,
            input_preview=preview(user_input),
        )
        rec.task_handle = asyncio.create_task(self._execute_run(rec, user_input))
        return web.json_response({"run_id": run_id, "session_id": session_id})

    async def handle_get_run(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        rec = self.runs.get(run_id)
        if rec is None:
            return web.json_response({"error": "Run not found"}, status=404)
        return web.json_response(
            {
                "run_id": rec.run_id,
                "session_id": rec.session_id,
                "status": rec.status,
                "output": rec.output,
                "error": rec.error,
            }
        )

    async def handle_stream_events(self, request: web.Request) -> web.StreamResponse:
        run_id = request.match_info["run_id"]
        rec = self.runs.get(run_id)
        if rec is None:
            return web.json_response({"error": "Run not found"}, status=404)

        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await resp.prepare(request)

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for past in rec.events:
            queue.put_nowait(past)
        rec.subscribers.add(queue)

        try:
            while True:
                event = await queue.get()
                payload = f"data: {json.dumps(event)}\n\n".encode("utf-8")
                await resp.write(payload)
                if event.get("event") in ("run.completed", "run.failed", "run.cancelled"):
                    break
        except asyncio.CancelledError:
            raise
        finally:
            rec.subscribers.discard(queue)
        return resp

    async def handle_telemetry_events(self, request: web.Request) -> web.StreamResponse:
        """SSE stream of transient daemon telemetry for the Ratatui dashboard (`brain top`)."""
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)

        queue = self.hub.subscribe()
        try:
            for past in self.hub.snapshot(100):
                await resp.write(f"data: {json.dumps(past)}\n\n".encode("utf-8"))
            await resp.write(b": brain telemetry connected\n\n")
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    self.hub.heartbeat(
                        pid=os.getpid(),
                        active_runs=sum(1 for r in self.runs.values() if r.status == "running"),
                    )
                    continue
                await resp.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.hub.unsubscribe(queue)
        return resp

    async def handle_telemetry_snapshot(self, request: web.Request) -> web.Response:
        """One-shot JSON snapshot for `curl` debugging and dashboard cold starts."""
        try:
            limit = int(request.query.get("limit", "100"))
        except ValueError:
            limit = 100
        state = self.db.get_state()
        return web.json_response(
            {
                "seq": self.hub.sequence,
                "uptime_s": round(self.hub.uptime_seconds, 1),
                "pid": os.getpid(),
                "mode": state.mode,
                "spent_today_usd": state.spent_today_usd,
                "daily_budget_usd": state.daily_budget_usd,
                "active_runs": [
                    {"run_id": r.run_id, "status": r.status} for r in self.runs.values()
                ],
                "events": self.hub.snapshot(limit),
            }
        )

    async def handle_outbox_resolve(self, request: web.Request) -> web.Response:
        """Approves or rejects a queued outbox item directly from the dashboard."""
        outbox_id = request.match_info["outbox_id"]
        body = await self._read_json_dict(request)
        # The cockpit sends `{"approved": true}`; scripts and the OpenAPI contract send `{"action": "approve"}`.
        if "approved" in body:
            approved = bool(body.get("approved"))
        else:
            approved = str(body.get("action") or "").lower() in ("approve", "approved", "once", "yes")
        row = self.db.resolve_approval(outbox_id, approved)
        if row is None:
            return web.json_response({"error": "Outbox item not found"}, status=404)
        if row.kind == "approval_request":
            self.hub.emit(
                EVENT_APPROVAL_RESPONDED,
                outbox_id=row.id,
                task_id=row.task_id,
                approved=approved,
                source="dashboard",
            )
        return web.json_response(
            {
                "ok": True,
                "id": row.id,
                "status": row.status,
                "task_id": row.task_id,
            }
        )

    async def handle_approval(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        rec = self.runs.get(run_id)
        if rec is None:
            return web.json_response({"error": "Run not found"}, status=404)

        body = await self._read_json_dict(request)
        approved = str(body.get("choice") or "").lower() == "once"

        if rec.pending_approval_future and not rec.pending_approval_future.done():
            rec.pending_approval_future.set_result(approved)
            rec.publish({"event": "approval.responded", "run_id": run_id, "approved": approved})
            self.hub.emit(
                EVENT_APPROVAL_RESPONDED,
                run_id=run_id,
                approved=approved,
                source="hermes",
            )
            return web.json_response({"ok": True})

        return web.json_response({"error": "No pending approval on this run"}, status=409)

    async def handle_stop_run(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        rec = self.runs.get(run_id)
        if rec is None:
            return web.json_response({"error": "Run not found"}, status=404)
        if rec.status in ("completed", "failed", "cancelled"):
            return web.json_response({"ok": True, "status": rec.status})

        self.reconciler.abort_run(run_id)
        if rec.pending_approval_future and not rec.pending_approval_future.done():
            rec.pending_approval_future.set_result(False)
        if rec.task_handle and not rec.task_handle.done():
            rec.task_handle.cancel()
        rec.status = "cancelled"
        rec.publish({"event": "run.cancelled", "run_id": run_id})
        self.hub.emit(EVENT_RUN_STATUS, run_id=run_id, status="cancelled", source="hermes")
        return web.json_response({"ok": True})

    async def handle_ask(self, request: web.Request) -> web.Response:
        """Synchronous iPhone Shortcut / Siri endpoint (`POST /v1/ask` -> `{"spoken": "..."}`)."""
        body = await self._read_json_dict(request)
        user_input = str(body.get("input") or "").strip()
        if not user_input:
            return web.json_response({"error": "input is required"}, status=400)

        self._prune_history()
        run_id = f"ask-{uuid.uuid4().hex[:12]}"
        rec = RunRecord(run_id=run_id, session_id=run_id)
        self.runs[run_id] = rec
        self.hub.emit(
            EVENT_HTTP_INBOUND,
            source="iphone_ask",
            route="/v1/ask",
            run_id=run_id,
            session_id=run_id,
            input_preview=preview(user_input),
        )
        await self._execute_run(rec, user_input, allow_live_approval=False)
        spoken = rec.output or rec.error or "Done."
        return web.json_response({"run_id": run_id, "status": rec.status, "spoken": spoken})

    async def _execute_run(
        self, rec: RunRecord, user_input: str, *, allow_live_approval: bool = True
    ) -> None:
        """Wraps the run body with live telemetry bookkeeping for the dashboard."""
        self.hub.emit(
            EVENT_RUN_STATUS,
            run_id=rec.run_id,
            session_id=rec.session_id,
            status="running",
            input_preview=preview(user_input),
        )
        try:
            await self._execute_run_inner(rec, user_input, allow_live_approval=allow_live_approval)
        finally:
            self.hub.emit(
                EVENT_RUN_STATUS,
                run_id=rec.run_id,
                session_id=rec.session_id,
                status=rec.status,
                output_preview=preview(rec.output),
                error=preview(rec.error),
            )
            self.hub.emit(
                EVENT_HTTP_COMPLETED,
                run_id=rec.run_id,
                session_id=rec.session_id,
                status=rec.status,
                spoken=preview(rec.output or rec.error),
            )

    async def _execute_run_inner(
        self, rec: RunRecord, user_input: str, *, allow_live_approval: bool = True
    ) -> None:
        try:
            # 1. Fast zero-token commands (mode switches, morning brief, explicit task trigger)
            if _AUTONOMOUS_RE.search(user_input):
                st = self.db.set_mode("AUTONOMOUS")
                ready_count = len(self.db.list_tasks(status="ready", mode="AUTONOMOUS"))
                rec.output = (
                    f"Switched to autonomous mode. {ready_count} ready autonomous task"
                    f"{'' if ready_count == 1 else 's'} queued, "
                    f"${st.budget_remaining_usd:.2f} budget remaining today."
                )
                rec.status = "completed"
                rec.publish({"event": "run.completed", "run_id": rec.run_id, "output": rec.output})
                return

            if _ASSIST_RE.search(user_input):
                st = self.db.set_mode("ASSIST")
                ready_count = len(self.db.list_tasks(status="ready", mode="ASSIST"))
                rec.output = (
                    f"Switched to assist mode. {ready_count} ready assist task"
                    f"{'' if ready_count == 1 else 's'} queued, "
                    f"${st.budget_remaining_usd:.2f} budget remaining today."
                )
                rec.status = "completed"
                rec.publish({"event": "run.completed", "run_id": rec.run_id, "output": rec.output})
                return

            if _BRIEF_RE.search(user_input):
                reports, pending_approval = self.db.drain_digest()
                st = self.db.get_state()
                lines = [item.summary_spoken for item in reports]
                if not lines:
                    lines.append(
                        f"Mode is {st.mode.lower()}, ${st.spent_today_usd:.2f} of "
                        f"${st.daily_budget_usd:.2f} spent today, no new overnight reports."
                    )
                summary_text = " ".join(lines)
                if pending_approval and allow_live_approval:
                    loop = asyncio.get_running_loop()
                    fut: asyncio.Future[bool] = loop.create_future()
                    rec.pending_outbox_id = pending_approval.id
                    rec.pending_approval_future = fut
                    rec.publish(
                        {
                            "event": "approval.request",
                            "run_id": rec.run_id,
                            "request_id": pending_approval.id,
                            "description": f"{summary_text} Next approval needed: {pending_approval.summary_spoken}",
                        }
                    )
                    try:
                        approved = await asyncio.wait_for(fut, timeout=120.0)
                    except asyncio.TimeoutError:
                        rec.output = f"{summary_text} Approval timed out; kept in queue."
                        rec.status = "completed"
                        rec.publish(
                            {"event": "run.completed", "run_id": rec.run_id, "output": rec.output}
                        )
                        return
                    finally:
                        rec.pending_outbox_id = None
                        rec.pending_approval_future = None

                    outbox_row = self.db.resolve_approval(pending_approval.id, approved)
                    if (
                        approved
                        and outbox_row is not None
                        and outbox_row.task_id is None
                        and outbox_row.resume_session_id
                    ):
                        self.db.mark_outbox_consumed(outbox_row.id)
                        resumed = await self.reconciler.run_interactive_planner(
                            user_input="The owner approved your deferred request. Proceed.",
                            run_key=rec.run_id,
                            resume_session_id=outbox_row.resume_session_id,
                            approval_cb=self._make_approval_cb(rec),
                        )
                        rec.output = f"Approved. {resumed.summary}".strip()
                    else:
                        state_word = "Approved" if approved else "Rejected"
                        task_note = (
                            f" task {outbox_row.task_id}"
                            if outbox_row and outbox_row.task_id
                            else ""
                        )
                        rec.output = f"{state_word}{task_note}."
                    rec.status = "completed"
                    rec.publish(
                        {"event": "run.completed", "run_id": rec.run_id, "output": rec.output}
                    )
                    return
                if pending_approval:
                    summary_text = f"{summary_text} Pending approval: {pending_approval.summary_spoken}"
                rec.output = summary_text
                rec.status = "completed"
                rec.publish({"event": "run.completed", "run_id": rec.run_id, "output": rec.output})
                return

            if _IMPROVE_RE.search(user_input):
                imp_res = await self.reconciler.run_skill_improver(run_key=rec.run_id)
                if imp_res.outcome == "errored":
                    rec.error = imp_res.summary
                    rec.status = "failed"
                    rec.publish({"event": "run.failed", "run_id": rec.run_id, "error": rec.error})
                else:
                    rec.output = imp_res.summary
                    rec.status = "completed"
                    rec.publish(
                        {"event": "run.completed", "run_id": rec.run_id, "output": rec.output}
                    )
                return

            task_match = _RUN_TASK_RE.search(user_input)
            if task_match:
                target_id = task_match.group(1).upper()
                claimed = self.db.claim_next_task(
                    lease_seconds=self.cfg.lease_seconds, task_id=target_id
                )
                if claimed is None:
                    rec.output = f"Task {target_id} is either not found, already running, or its repository is busy."
                    rec.status = "completed"
                    rec.publish(
                        {"event": "run.completed", "run_id": rec.run_id, "output": rec.output}
                    )
                    return
                finished = await self.reconciler.execute_claimed_task(
                    claimed,
                    run_key=rec.run_id,
                    approval_cb=(self._make_approval_cb(rec) if allow_live_approval else None),
                )
                restart_note = " Restarting Brain daemon now." if self.reconciler.restart_pending else ""
                rec.output = f"Task {finished.id} finished with status {finished.status}.{restart_note}"
                rec.status = "completed"
                rec.publish({"event": "run.completed", "run_id": rec.run_id, "output": rec.output})
                self._schedule_restart_if_pending()
                return

            # 2. Interactive Planner turn
            res = await self.reconciler.run_interactive_planner(
                user_input=user_input,
                run_key=rec.run_id,
                resume_session_id=self._pi_sessions.get(rec.session_id),
                approval_cb=(self._make_approval_cb(rec) if allow_live_approval else None),
            )
            if res.session_id:
                self._pi_sessions[rec.session_id] = res.session_id
            if res.outcome == "errored":
                rec.error = res.summary
                rec.status = "failed"
                rec.publish({"event": "run.failed", "run_id": rec.run_id, "error": rec.error})
                return

            # 3. If Planner queued a ready self-improvement task for repo='brain', run it immediately
            self_notes: list[str] = []
            for tid in res.upserted_task_ids or []:
                t = self.db.get_task(tid)
                if t.repo == "brain" and t.status == "ready" and t.verify_cmd:
                    claimed_self = self.db.claim_next_task(
                        lease_seconds=self.cfg.lease_seconds, task_id=t.id
                    )
                    if claimed_self is not None:
                        done_self = await self.reconciler.execute_claimed_task(
                            claimed_self,
                            run_key=rec.run_id,
                            approval_cb=(
                                self._make_approval_cb(rec) if allow_live_approval else None
                            ),
                        )
                        if done_self.status == "done":
                            self_notes.append(
                                f"Self-upgrade {done_self.id} passed verification and was applied."
                            )
                        else:
                            self_notes.append(
                                f"Self-upgrade {done_self.id} ended with status {done_self.status}."
                            )

            if self.reconciler.restart_pending:
                self_notes.append("Restarting Brain daemon now.")
            rec.output = " ".join([res.summary, *self_notes]).strip()
            rec.status = "completed"
            rec.publish({"event": "run.completed", "run_id": rec.run_id, "output": rec.output})
            self._schedule_restart_if_pending()
        except asyncio.CancelledError:
            rec.status = "cancelled"
            rec.publish({"event": "run.cancelled", "run_id": rec.run_id})
        except Exception as exc:
            rec.error = str(exc)
            rec.status = "failed"
            rec.publish({"event": "run.failed", "run_id": rec.run_id, "error": rec.error})

    def _schedule_restart_if_pending(self) -> None:
        if self.reconciler.restart_pending and self.reconciler.on_restart_requested is not None:
            self.reconciler.restart_pending = False
            loop = asyncio.get_running_loop()
            loop.call_later(0.5, self.reconciler.on_restart_requested)

    def _make_approval_cb(
        self, rec: RunRecord
    ) -> Any:
        async def _cb(
            summary_spoken: str, _details: str, _session_id: str | None
        ) -> bool | None:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[bool] = loop.create_future()
            rec.pending_approval_future = fut
            req_id = f"req-{uuid.uuid4().hex[:8]}"
            rec.publish(
                {
                    "event": "approval.request",
                    "run_id": rec.run_id,
                    "request_id": req_id,
                    "description": summary_spoken,
                }
            )
            self.hub.emit(
                EVENT_APPROVAL_REQUEST,
                run_id=rec.run_id,
                request_id=req_id,
                description=preview(summary_spoken),
                source="hermes",
            )
            try:
                return await asyncio.wait_for(fut, timeout=120.0)
            except asyncio.TimeoutError:
                return None
            finally:
                rec.pending_approval_future = None

        return _cb
