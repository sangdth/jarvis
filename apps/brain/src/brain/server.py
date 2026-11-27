from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from aiohttp import web
from loguru import logger

from brain import memory, schedule
from brain.config import Settings, settings
from brain.db import BrainDB, OutboxRow
from brain.orchestrator import BrainOrchestrator
from brain.reconciler import Reconciler
from brain.runners import ClaudeRunner, EphemeralPiRunner, OpenCodeRunner, Runner, T3Runner
from brain.telemetry import (
    EVENT_APPROVAL_REQUEST,
    EVENT_APPROVAL_RESPONDED,
    EVENT_HTTP_COMPLETED,
    EVENT_HTTP_INBOUND,
    EVENT_PAIR_REQUESTED,
    EVENT_PAIR_RESOLVED,
    EVENT_RUN_STATUS,
    TelemetryHub,
    preview,
)
from brain.traces import TraceSink
from jarvis_core.devices import DeviceStore
from jarvis_core.home import JARVIS_HOME, REPO_ROOT, git_version
from jarvis_core.net import advertised_hosts
from jarvis_core.questions import is_approval

# A pairing code is short, single-use and short-lived; five wrong guesses in a minute stops the guessing.
PAIR_TTL_SECS = 300
PAIR_MAX_FAILURES = 5
PAIR_FAILURE_WINDOW_SECS = 60.0
PAIR_EMIT_COOLDOWN_SECS = 5.0

_CMD_PREFIX = r"^\s*(?:(?:hey\s+)?jarvis[,\s]+)?(?:please\s+)?"
_AUTONOMOUS_RE = re.compile(
    _CMD_PREFIX
    + r"(?:(?:switch|go|put\s+it)\s+(?:to|into)\s+autonomous(?:\s+mode)?"
    + r"|start\s+(?:the\s+)?night\s+(?:mode|run)|autonomous\s+mode)\s*[.!]*\s*$",
    re.IGNORECASE,
)
_ASSIST_RE = re.compile(
    _CMD_PREFIX
    + r"(?:(?:switch|go|put\s+it)\s+(?:back\s+)?(?:to|into)\s+assist(?:\s+mode)?"
    + r"|(?:pause|hold)\s+background(?:\s+work|\s+tasks)?|assist\s+mode|day\s+mode)\s*[.!]*\s*$",
    re.IGNORECASE,
)
_BRIEF_RE = re.compile(
    _CMD_PREFIX
    + r"(?:(?:give\s+me\s+(?:the\s+)?|read\s+(?:me\s+)?(?:the\s+)?|what(?:'s|\s+is)\s+(?:the\s+)?)?"
    + r"(?:morning\s+brief|brain\s+status|status\s+report)|what\s+happened\s+overnight)\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_IMPROVE_RE = re.compile(
    _CMD_PREFIX
    + r"(?:improve\s+skills|learn\s+from\s+recent\s+runs|reflect\s+on\s+recent\s+runs|(?:run\s+)?self[\s-]+improve)\s*[.!]*\s*$",
    re.IGNORECASE,
)
_RUN_TASK_RE = re.compile(
    _CMD_PREFIX + r"run\s+(?:task\s+)?((?:SG|PRB)-\d+(?:\.\d+)?)\s*[.!]*\s*$",
    re.IGNORECASE,
)
_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DURATION_WORDS = r"\d+(?:\.\d+)?\s*(?:seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)"
_REMIND_RE = re.compile(
    r"^\s*(?:jarvis[,\s]+)?(?:please\s+)?remind\s+me(?:\s+to)?\s+"
    r"(?P<body>.+?)\s+(?:in|after)\s+(?P<duration>" + _DURATION_WORDS + r")\s*[.!]?$",
    re.IGNORECASE,
)
_CHECK_IN_RE = re.compile(
    r"^\s*(?:jarvis[,\s]+)?(?:please\s+)?(?:check(?:\s+back)?|follow\s+up)"
    r"(?:\s+(?:on|about)\s+(?P<body>.+?))?\s+in\s+"
    r"(?P<duration>" + _DURATION_WORDS + r")\s*(?:\s+(?:on|about)\s+(?P<body2>.+?))?\s*[.!]?$",
    re.IGNORECASE,
)


@dataclass
class RunRecord:
    run_id: str
    session_id: str
    status: str = "running"  # 'running' | 'completed' | 'failed' | 'cancelled'
    output: str = ""
    error: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    pending_approval_future: asyncio.Future[str | None] | None = None
    pending_outbox_id: str | None = None
    pending_question: dict[str, Any] | None = None
    pinned: dict[str, str] | None = None
    session_mode: str | None = None  # "new", or "continuing" (a picked session for this task)
    cwd: str | None = None  # a folder the voice already resolved (project picker)
    chat_session_key: str | None = None  # the `pi` conversation this voice session continues
    turn_id: str = ""
    trace_id: str = ""
    task_handle: asyncio.Task[None] | None = None
    mirror: Callable[[dict[str, Any]], None] | None = None

    def publish(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty, asyncio.QueueFull):
                    q.get_nowait()
                    q.put_nowait(event)
        if self.mirror is not None:
            self.mirror(event)


class BrainServer:
    """
    The one Jarvis network surface: the brain's HTTP/SSE contract, the thin client's mic and speaker routes, the
    dashboard's telemetry, and the small operator API a probe or a script needs (`/health`, `/v1/status`, `/v1/board`,
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
        runners: dict[str, Runner] | None = None,
        orchestrator: BrainOrchestrator | None = None,
    ) -> None:
        self.db = db
        self.reconciler = reconciler
        self.cfg = cfg
        self.hub = hub if hub is not None else reconciler.hub
        self.runs: dict[str, RunRecord] = {}
        self._pi_sessions: dict[str, str] = {}
        self.devices = devices if devices is not None else DeviceStore.load()
        self.runners: dict[str, Runner] = runners if runners is not None else {
            "ephemeral": EphemeralPiRunner(cfg, db),
            "t3": T3Runner(cfg, db),
            "claude": ClaudeRunner(cfg),
            "opencode": OpenCodeRunner(cfg),
        }
        t3_runner = self.runners.get("t3")
        if t3_runner is not None and hasattr(t3_runner, "on_unsolicited"):
            t3_runner.on_unsolicited = self._publish_global  # type: ignore[attr-defined]
        self.orchestrator = orchestrator or BrainOrchestrator(
            cfg, db, reconciler, runners=self.runners, last_project=self._last_project
        )
        self._global_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._stopping = False
        self._last_project_by_session: dict[str, str] = {}
        # One pairing code at a time, in memory: a restart clears it, which is what we want.
        self._pair_codes: dict[str, float] = {}
        self._pair_name = ""  # who asked, for the dashboard's banner; the app that asked first keeps it
        self._pair_last_emitted = 0.0
        self._pair_failures: deque[float] = deque(maxlen=PAIR_MAX_FAILURES * 2)
        self._voice_snapshot = voice_snapshot or (lambda: {"state": "not running"})
        self._started_at = started_at if started_at is not None else time.time()
        self._version = git_version()  # pinned at boot so `/v1/status` reports what is running, not a newer pull
        self.traces = TraceSink(self.db, self.cfg, self.hub)
        memory.ensure_layout(self.cfg.home_dir)
        self.db.subscribe_outbox(self._on_outbox)
        self.app = web.Application(middlewares=[self._auth_middleware])
        self.app.add_routes(
            [
                web.get("/health", self.handle_health),
                web.post("/v1/pair/start", self.handle_pair_start),
                web.post("/v1/pair/request", self.handle_pair_request),
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
                web.get("/v1/sessions", self.handle_sessions),
                web.post("/v1/connect", self.handle_connect),
                web.get("/v1/traces", self.handle_traces),
                web.get("/v1/traces/diff", self.handle_trace_diff),
                web.get("/v1/traces/{trace_id}", self.handle_trace_detail),
                web.post("/v1/runs", self.handle_create_run),
                web.get("/v1/runs/{run_id}", self.handle_get_run),
                web.get("/v1/runs/{run_id}/events", self.handle_stream_events),
                web.post("/v1/runs/{run_id}/approval", self.handle_approval),
                web.post("/v1/runs/{run_id}/answer", self.handle_answer),
                web.post("/v1/runs/{run_id}/stop", self.handle_stop_run),
                web.post("/v1/ask", self.handle_ask),
                web.get("/v1/events", self.handle_events),
                web.get("/v1/outbox", self.handle_outbox_list),
                web.post("/v1/outbox/{outbox_id}/spoken", self.handle_outbox_spoken),
                web.get("/v1/timers", self.handle_timers),
                web.post("/v1/timers", self.handle_add_timer),
                web.post("/v1/timers/{timer_id}/cancel", self.handle_cancel_timer),
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
        if request.headers.get("Origin"):
            # Jarvis has no web client on this port; rejecting browser Origins blocks cross-site CSRF and WS hijacking.
            return web.json_response({"error": "Browser origins are not allowed"}, status=403)
        if request.path == "/health":
            return await handler(request)  # the liveness probe: no secret, and no state
        if request.method == "POST" and request.path in ("/v1/pair", "/v1/pair/request"):
            return await handler(request)  # the bootstrap: a client with no secret yet, and the handler guards itself
        if request.path == "/client.py":
            return await handler(request)  # the script that pairs: it carries no secret, only this server's address
        if request.path in ("/Jarvis.app.zip", "/install-app"):
            return await handler(request)  # the mac app and its installer: a binary and a bootstrap, no secret in either
        if request.path == "/ws":
            return await handler(request)  # the relay checks its own credential, shared token or device secret
        token = self.request_token(request)
        if self._is_owner(token):
            return await handler(request)
        if self.devices.verify(token) is not None:
            if request.method == "GET" and request.path == "/v1/status":
                return await handler(request)
            return web.json_response({"error": "owner token required"}, status=401)
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

    def _chat_key_for(self, session_id: str, session_mode: str | None) -> str:
        """The `pi` conversation key behind a voice session; `new` starts a fresh transcript.

        The voice's own `session_id` is stable for its whole run, so the key survives a brain restart. A
        `new session` bumps a generation suffix stored in memory; after a restart it returns to generation 0.
        """
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", session_id)[:40].strip(".-") or "chat"
        if session_mode == "new":
            self._pi_sessions[session_id] = uuid.uuid4().hex[:8]
        generation = self._pi_sessions.get(session_id, "0")
        return f"{safe}-{generation}"

    async def start_background(self) -> None:
        """Start the long-lived runners (T3's socket; the others are lazy) and the trace sink."""
        self.traces.start()
        for runner in self.runners.values():
            try:
                await runner.start()
            except Exception as exc:
                logger.warning(f"[Brain] runner {runner.id} did not start: {exc!r}")

    async def stop_background(self) -> None:
        self._stopping = True
        await self.traces.stop()
        self._publish_global({})
        for queue in list(self.hub._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait({})
        for runner in self.runners.values():
            try:
                await runner.stop()
            except Exception:
                pass
        await self.orchestrator.close()

    def _last_project(self, session_id: str) -> str | None:
        return self._last_project_by_session.get(session_id)

    def _on_outbox(self, row: OutboxRow) -> None:
        """A new item reached the outbox. Deliver after the writing transaction commits, never inside it."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # a synchronous script/tests: there is no voice to notify
        loop.call_soon(self._deliver_outbox, row)

    def _deliver_outbox(self, row: OutboxRow) -> None:
        mode = self.db.get_state().mode
        payload = {
            "event": "outbox.queued",
            "outbox": self._outbox_dict(row),
            "mode": mode,
        }
        self.hub.emit("outbox.queued", outbox_id=row.id, kind=row.kind, mode=mode)
        if mode == "ASSIST":
            self._publish_global(payload)

    def _announce_queued_outbox(self) -> None:
        """Switching back to ASSIST: hand the night's queue to any connected voice now."""
        for row in self.db.list_outbox(limit=100):
            if row.status == "queued":
                self._deliver_outbox(row)

    def _publish_global(self, event: dict[str, Any]) -> None:
        for queue in list(self._global_subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty, asyncio.QueueFull):
                    queue.get_nowait()
                    queue.put_nowait(event)

    async def handle_events(self, request: web.Request) -> web.StreamResponse:
        """The daemon-to-voice stream: events no voice-initiated run is waiting on (a T3 thread finished,
        a background task asks for approval). The voice speaks them under the same mute rules."""
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._global_subscribers.add(queue)
        try:
            await resp.write(b": brain events connected\n\n")
            while not self._stopping:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
                    continue
                if self._stopping:
                    break
                await resp.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._global_subscribers.discard(queue)
        return resp

    async def handle_sessions(self, request: web.Request) -> web.Response:
        """List a runner's sessions/threads, newest first, for `jarvis connect` and the model."""
        runner_id = request.query.get("runner", "t3")
        runner = self.runners.get(runner_id)
        if runner is None:
            return web.json_response({"error": f"Unknown runner {runner_id!r}"}, status=404)
        project = request.query.get("project")
        cwd = None
        if project:
            from jarvis_core.projects import resolve_project

            folder = resolve_project(project)
            if folder is None:
                return web.json_response({"error": f"Unknown project {project!r}"}, status=404)
            cwd = str(folder)
        try:
            limit = max(1, min(int(request.query.get("limit", "20")), 200))
        except ValueError:
            limit = 20
        try:
            sessions = await runner.list_sessions(cwd, limit=limit)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=503)
        return web.json_response({
            "runner": runner_id,
            "sessions": [
                {"id": s.id, "title": s.title, "cwd": s.cwd, "branch": s.branch,
                 "running": s.running, "updated_at": s.updated_at,
                 "visited_at": getattr(s, "visited_at", 0.0)}
                for s in sessions
            ],
        })

    async def handle_connect(self, request: web.Request) -> web.Response:
        """`jarvis connect`: pin one session for the run, answer its prompt, or list what can be picked."""
        body = await self._read_json_dict(request)
        runner_id = str(body.get("runner") or "t3")
        runner = self.runners.get(runner_id)
        if runner is None:
            return web.json_response({"error": f"Unknown runner {runner_id!r}"}, status=404)
        session_id = str(body.get("session_id") or "").strip()
        answer = str(body.get("answer") or "").strip()
        if session_id and answer:
            ok = await runner.answer(session_id, answer)
            return web.json_response({"ok": ok})
        project_root = str(body.get("project_root") or "").strip()
        worktree_path = str(body.get("worktree_path") or "").strip() or None
        project = str(body.get("project") or "").strip() or None
        try:
            if (project_root or worktree_path) and hasattr(runner, "resolve_folder_session"):
                chosen = await runner.resolve_folder_session(project_root, worktree_path)  # type: ignore[attr-defined]
                if chosen is None:
                    return web.json_response({"error": "Session not found"}, status=404)
                pending = await runner.watch_session(chosen.id) if hasattr(runner, "watch_session") else None  # type: ignore[attr-defined]
                return web.json_response({
                    "pinned": {"runner": runner_id, "session_id": chosen.id},
                    "session": {"id": chosen.id, "title": chosen.title, "cwd": chosen.cwd,
                                "branch": chosen.branch, "running": chosen.running},
                    **({"pending": pending} if pending else {}),
                })
            sessions = await runner.list_sessions(None, limit=200)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=503)
        if session_id:
            chosen = next((s for s in sessions if s.id == session_id), None)
            if chosen is None:
                return web.json_response({"error": "Session not found"}, status=404)
            pending = await runner.watch_session(chosen.id) if hasattr(runner, "watch_session") else None  # type: ignore[attr-defined]
            return web.json_response({
                "pinned": {"runner": runner_id, "session_id": chosen.id},
                "session": {"id": chosen.id, "title": chosen.title, "cwd": chosen.cwd,
                            "branch": chosen.branch, "running": chosen.running},
                **({"pending": pending} if pending else {}),
            })
        if project:
            from jarvis_core.projects import resolve_project

            folder = resolve_project(project)
            if folder is not None:
                sessions = [s for s in sessions if s.cwd == str(folder)]
        return web.json_response({
            "runner": runner_id,
            "sessions": [
                {"id": s.id, "title": s.title, "cwd": s.cwd, "branch": s.branch, "running": s.running}
                for s in sessions[:20]
            ],
        })


    # ------------------------------------------------------------------ traces

    @staticmethod
    def _trace_dict(row: Any) -> dict[str, Any]:
        return {
            "trace_id": row.trace_id,
            "turn_id": row.turn_id,
            "source": row.source,
            "surface": row.surface,
            "runner": row.runner,
            "repo": row.repo,
            "task_id": row.task_id,
            "attempt_id": row.attempt_id,
            "outcome": row.outcome,
            "friction_kind": row.friction_kind,
            "causal_sig": row.causal_sig,
            "latency_ms": row.latency_ms,
            "cost_usd": row.cost_usd,
            "git_sha": row.git_sha,
            "prompt_sha": row.prompt_sha,
            "skills_used": list(row.skills_used),
            "user_input": row.user_input,
            "output_summary": row.output_summary,
            "trace_path": row.trace_path,
            "superseded_by": row.superseded_by,
            "reflected": bool(row.reflected),
            "created_at": row.created_at,
        }

    async def handle_traces(self, request: web.Request) -> web.Response:
        """Recent turns and runs, searchable; the same rows the Improver mines and `jarvis traces` prints."""
        try:
            limit = max(1, min(int(request.query.get("limit", "30")), 200))
        except ValueError:
            limit = 30
        rows = self.db.list_trace_runs(
            limit=limit,
            outcome=(request.query.get("outcome") or "").strip() or None,
            surface=(request.query.get("surface") or "").strip() or None,
            query=(request.query.get("q") or "").strip() or None,
            reflected=(0 if request.query.get("unreflected") else None),
        )
        return web.json_response({"traces": [self._trace_dict(row) for row in rows]})

    async def handle_trace_detail(self, request: web.Request) -> web.Response:
        trace_id = request.match_info["trace_id"]
        row = self.db.get_trace_run(trace_id)
        if row is None:
            return web.json_response({"error": "Trace not found"}, status=404)
        detail: dict[str, Any] = {"trace": self._trace_dict(row)}
        path = (self.cfg.traces_dir / row.trace_path).resolve() if row.trace_path else None
        root = self.cfg.traces_dir.resolve()
        if path is not None and root in path.parents and path.is_file():
            try:
                detail["record"] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                detail["record_error"] = str(exc)
        return web.json_response(detail)

    async def handle_trace_diff(self, request: web.Request) -> web.Response:
        """Per-commit health, so a self-upgrade that quietly degraded routing or latency is visible."""
        base = (request.query.get("base") or "").strip()
        head = (request.query.get("head") or "").strip()
        stats = self.db.trace_sha_stats()
        if base or head:
            stats = [s for s in stats if s["git_sha"].startswith(base) or s["git_sha"].startswith(head)]
        return web.json_response({"stats": stats})

    def _task_dict(self, t: Any) -> dict[str, Any]:        return {
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

    def _timer_dict(self, t: Any) -> dict[str, Any]:
        return {
            "id": t.id,
            "kind": t.kind,
            "action": t.action,
            "cron_expr": t.cron_expr,
            "timezone": t.timezone,
            "next_run_at": t.next_run_at,
            "repo": t.repo,
            "mode": t.mode,
            "title": t.title,
            "prompt": t.prompt_spec,
            "verify_cmd": t.verify_cmd,
            "max_cost_usd": t.max_cost_usd,
            "created_by": t.created_by,
            "status": t.status,
            "task_id": t.task_id,
            "chain_count": t.chain_count,
            "created_at": t.created_at,
            "updated_at": t.updated_at,
        }

    def status_payload(self) -> dict[str, Any]:
        """Everything a probe, `jarvis status`, or a phone needs: version, uptime, mode, spend, counts, voice."""
        st = self.db.get_state()
        tasks = self.db.list_tasks()
        skills = self.db.list_skills(status="active")
        return {
            "ok": True,
            "version": self._version,
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
            "approvals_waiting": self.db.count_outbox(status="queued", kind="approval_request"),
            "timers_active": sum(1 for t in self.db.list_timers(status="active")),
            "outbox_queued": self.db.count_outbox(status="queued"),
            "telemetry_seq": self.hub.sequence,
            "voice": self._voice_snapshot(),
        }

    async def handle_health(self, request: web.Request) -> web.Response:
        """Unauthenticated liveness probe: is a Jarvis answering here, and which build?"""
        return web.json_response(
            {
                "ok": True,
                "service": "jarvis",
                "version": self._version,
                "pid": os.getpid(),
                "uptime_s": round(time.time() - self._started_at, 1),
            }
        )

    async def handle_status(self, request: web.Request) -> web.Response:
        return web.json_response(self.status_payload())

    def _reveal_pair_code(self, name: str, *, throttle_emit: bool = False) -> tuple[str, float]:
        """
        One pending code at a time, and it shows where the owner can see it: the dashboard's telemetry, or `jarvis pair`.

        A second request reuses the code that is still live instead of minting a new one, so a double click, or an app
        asking again while you type, never steals the digits on your screen.
        """
        now = time.time()
        active = {code: expires for code, expires in self._pair_codes.items() if expires > now}
        reused = bool(active)
        if active:
            code, expires = next(iter(active.items()))
            label = name or self._pair_name
        else:
            code, expires = f"{secrets.randbelow(1_000_000):06d}", now + PAIR_TTL_SECS
            self._pair_codes = {code: expires}
            self._pair_failures.clear()
            self._pair_name = label = name
            logger.info("[Pair] a pairing code was issued (single use, 5 minutes); `jarvis dashboard` shows it.")
        if not (reused and throttle_emit and now - self._pair_last_emitted < PAIR_EMIT_COOLDOWN_SECS):
            self._pair_last_emitted = now
            self.hub.emit(
                EVENT_PAIR_REQUESTED,
                code=code,
                name=label or "A device",
                expires_at=round(expires, 3),
                ttl_s=max(0, int(expires - now)),
            )
        return code, expires

    async def handle_pair_start(self, request: web.Request) -> web.Response:
        """`jarvis pair`: mint a short code and print it here, on the one terminal only the owner reads."""
        code, expires = self._reveal_pair_code("")
        hosts = advertised_hosts()
        server = f"http://{hosts[0]}:{self.cfg.brain_port}" if hosts else self.cfg.endpoint_url
        return web.json_response(
            {"code": code, "expires_in": max(0, int(expires - time.time())), "server": server}
        )

    async def handle_pair_request(self, request: web.Request) -> web.Response:
        """
        An unpaired client asks for a code; the code goes to the dashboard, never back down this connection.

        Open on purpose, like `GET /client.py`: an app that has never paired holds no secret yet. Asking costs
        nothing and reveals nothing — reading the digits takes the owner's token, or the screen of the Mac it runs on.
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = " ".join(str(body.get("name") or "").split())[:60] if isinstance(body, dict) else ""
        _, expires = self._reveal_pair_code(name, throttle_emit=True)
        return web.json_response({"ok": True, "expires_in": max(0, int(expires - time.time()))})

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
            self.hub.emit(EVENT_PAIR_RESOLVED, ok=False, reason="too many wrong codes")
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
        self.hub.emit(EVENT_PAIR_RESOLVED, ok=True, device=device.name, device_id=device.id)
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
        """The full board snapshot in one body: what a remote dashboard draws, with no direct SQLite access."""
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
                "attempts": [self._attempt_dict(a) for a in self.db.list_attempts(limit=400)],
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
        self.reconciler.wake()
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
        if st.mode == "ASSIST":
            # The night's quiet queue becomes speakable the moment the owner switches back.
            self._announce_queued_outbox()
        return web.json_response({"ok": True, "mode": st.mode, "budget_remaining_usd": round(st.budget_remaining_usd, 4)})

    async def handle_improve(self, request: web.Request) -> web.Response:
        res = await self.reconciler.run_skill_improver()
        return web.json_response(
            {
                "outcome": res.outcome,
                "summary": res.summary,
                "modified": list(res.modified_skills or []),
                "modified_memory": list(res.modified_memory or []),
            }
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
        raw_pinned = body.get("pinned")
        pinned = (
            {"runner": str(raw_pinned.get("runner") or "t3"),
             "session_id": str(raw_pinned.get("session_id") or "")}
            if isinstance(raw_pinned, dict) and raw_pinned.get("session_id")
            else None
        )
        raw_trace = str(body.get("trace_id") or "").strip()
        safe_trace = raw_trace if _TRACE_ID_RE.fullmatch(raw_trace) else uuid.uuid4().hex
        rec = RunRecord(run_id=run_id, session_id=session_id, pinned=pinned,
                        session_mode=str(body.get("session") or "") or None,
                        cwd=str(body.get("cwd") or "") or None,
                        turn_id=str(body.get("turn_id") or "") or "",
                        trace_id=safe_trace)
        rec.chat_session_key = self._chat_key_for(session_id, rec.session_mode)
        self.runs[run_id] = rec
        replace = bool(body.get("replace"))
        if replace:
            for other in list(self.runs.values()):
                if (
                    other is not rec
                    and other.session_id == session_id
                    and other.status == "running"
                ):
                    self.traces.mark_replaced(
                        other.trace_id,
                        rec.trace_id,
                        old_project=self._last_project_by_session.get(session_id, ""),
                        new_project=str(body.get("cwd") or ""),
                    )
                    await self._stop_run(other, reason="Replaced by a corrected task.", replaced=True)
        self.traces.note_run_created(
            trace_id=rec.trace_id,
            turn_id=rec.turn_id,
            session_id=session_id,
            user_input=user_input,
            project=str(body.get("cwd") or self._last_project_by_session.get(session_id, "")),
            runner="",
            replace=replace,
        )
        screenshot = str(body.get("screenshot") or "") or None
        self.hub.emit(
            EVENT_HTTP_INBOUND,
            source="voice",
            route="/v1/runs",
            run_id=run_id,
            session_id=session_id,
            trace_id=rec.trace_id,
            turn_id=rec.turn_id,
            input_preview=preview(user_input),
        )
        rec.task_handle = asyncio.create_task(self._execute_run(rec, user_input, screenshot=screenshot))
        return web.json_response({"run_id": run_id, "session_id": session_id})

    async def handle_answer(self, request: web.Request) -> web.Response:
        """Answer the approval or question the active run is waiting on. Approvals never go through an LLM."""
        run_id = request.match_info["run_id"]
        rec = self.runs.get(run_id)
        if rec is None:
            return web.json_response({"error": "Run not found"}, status=404)
        body = await self._read_json_dict(request)
        answer = str(body.get("answer") or "").strip()
        if not answer:
            return web.json_response({"error": "answer is required"}, status=400)
        if rec.pending_approval_future is None or rec.pending_approval_future.done():
            return web.json_response({"error": "No pending question on this run"}, status=409)
        rec.pending_approval_future.set_result(answer)
        rec.publish({"event": "approval.responded", "run_id": run_id, "request_id": body.get("request_id")})
        self.hub.emit(EVENT_APPROVAL_RESPONDED, run_id=run_id, approved=None, source="voice")
        return web.json_response({"ok": True})

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

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        for past in rec.events[-256:]:
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
        """SSE stream of transient daemon telemetry for the Ratatui dashboard (`jarvis dashboard`)."""
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
            while not self._stopping:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    self.hub.heartbeat(
                        pid=os.getpid(),
                        active_runs=sum(1 for r in self.runs.values() if r.status == "running"),
                    )
                    continue
                if self._stopping:
                    break
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
        # The dashboard sends `{"approved": true}`; scripts and the OpenAPI contract send `{"action": "approve"}`.
        if "approved" in body:
            approved = bool(body.get("approved"))
        else:
            approved = str(body.get("action") or "").lower() in ("approve", "approved", "once", "yes")
        row = self.db.resolve_approval(outbox_id, approved)
        if row is None:
            return web.json_response({"error": "Outbox item not found"}, status=404)
        if not approved and row.task_id:
            # A rejected proposal is remembered: the Improver may not re-queue the same idea unchanged.
            self.db.set_harness_outcome(task_id=row.task_id, outcome="rejected")
        if row.kind == "approval_request":
            self.hub.emit(
                EVENT_APPROVAL_RESPONDED,
                outbox_id=row.id,
                task_id=row.task_id,
                approved=approved,
                source="dashboard",
            )
        try:
            details = json.loads(row.details_json or "{}")
        except ValueError:
            details = {}
        if approved and isinstance(details, dict) and details.get("rollback_sha"):
            self.db.mark_outbox_consumed(row.id)
            raw_pid = details.get("proposal_id")
            proposal_id = int(raw_pid) if raw_pid is not None else None
            rolled_back = await self.reconciler.apply_rollback(
                str(details["rollback_sha"]), proposal_id=proposal_id
            )
            self._schedule_restart_if_pending()
            return web.json_response(
                {
                    "ok": True,
                    "id": row.id,
                    "status": row.status,
                    "task_id": row.task_id,
                    "rolled_back": rolled_back,
                }
            )
        if approved and row.task_id:
            self.reconciler.wake()
        answer_text = str(body.get("answer") or ("yes" if approved else "no")).strip()
        if isinstance(details, dict) and details.get("runner") and details.get("session_id"):
            await self._forward_runner_outbox(row, details, answer_text)
        if approved and row.task_id is None and row.resume_session_id:
            # A deferred interactive Planner turn: resume the exact `pi` session that parked itself, without
            # holding this HTTP response (and the voice) open for the whole turn.
            self.db.mark_outbox_consumed(row.id)
            asyncio.create_task(
                self._resume_approved_outbox(row), name=f"resume-{row.id}"
            )
        return web.json_response(
            {
                "ok": True,
                "id": row.id,
                "status": row.status,
                "task_id": row.task_id,
            }
        )

    async def _forward_runner_outbox(
        self, row: OutboxRow, details: dict[str, Any], answer_text: str
    ) -> bool:
        runner = self.runners.get(str(details.get("runner") or ""))
        session_id = str(details.get("session_id") or "").strip()
        if runner is None or not session_id:
            return False
        self.db.mark_outbox_consumed(row.id)
        try:
            return await runner.answer(session_id, answer_text)
        except Exception as exc:
            logger.error(f"[Brain] forwarding outbox {row.id} to {runner.id} failed: {exc!r}")
            return False

    async def _resume_approved_outbox(self, row: OutboxRow) -> None:
        try:
            resumed = await self.reconciler.run_interactive_planner(
                user_input="The owner approved your deferred request. Proceed.",
                run_key=f"ob-{row.id}",
                resume_session_id=row.resume_session_id,
            )
            self.db.queue_outbox(
                kind="report",
                summary_spoken=resumed.summary,
                details={"resumed_from": row.id, "outcome": resumed.outcome},
            )
        except Exception as exc:
            logger.error(f"[Brain] resuming outbox {row.id} failed: {exc!r}")
            self.db.queue_outbox(
                kind="report",
                summary_spoken=f"Resuming the approved turn failed: {exc}",
                details={"resumed_from": row.id, "reason": "resume_error"},
            )

    async def handle_outbox_list(self, request: web.Request) -> web.Response:
        """Queued items for a reconnecting voice; `status=spoken` and the rest are available to scripts."""
        status = (request.query.get("status") or "queued").strip()
        valid = {"queued", "spoken", "approved", "rejected"}
        if status not in valid:
            return web.json_response({"error": f"status must be one of {sorted(valid)}"}, status=400)
        try:
            limit = int(request.query.get("limit", "100"))
        except ValueError:
            limit = 100
        rows = [row for row in self.db.list_outbox(limit=max(1, min(limit, 500))) if row.status == status]
        return web.json_response({"outbox": [self._outbox_dict(row) for row in rows]})

    async def handle_outbox_spoken(self, request: web.Request) -> web.Response:
        """The voice took an item. Only after this is it out of the queued list and in `reported.md`."""
        outbox_id = request.match_info["outbox_id"]
        before = self.db.get_outbox(outbox_id)
        row = self.db.mark_outbox_spoken(outbox_id)
        if row is None:
            return web.json_response({"error": "Outbox item not found"}, status=404)
        if before is not None and before.status == "queued":
            memory.append_reported(
                self.cfg.home_dir,
                f"[{row.kind}] {row.summary_spoken}",
            )
        return web.json_response({"ok": True, "id": row.id, "status": row.status})

    async def handle_timers(self, request: web.Request) -> web.Response:
        status = (request.query.get("status") or "").strip()
        valid = {"active", "firing", "paused", "done"}
        if status and status not in valid:
            return web.json_response({"error": f"status must be one of {sorted(valid)}"}, status=400)
        rows = self.db.list_timers(status=status or None)  # type: ignore[arg-type]
        return web.json_response({"timers": [self._timer_dict(t) for t in rows]})

    async def handle_add_timer(self, request: web.Request) -> web.Response:
        """Create a follow-up (`in`/`at`) or a schedule (`every`); the reconciler fires it."""
        body = await self._read_json_dict(request)
        every = str(body.get("every") or "").strip()
        in_text = str(body.get("in") or "").strip()
        at_text = str(body.get("at") or "").strip()
        chosen = [value for value in (every, in_text, at_text) if value]
        if len(chosen) != 1:
            return web.json_response({"error": "give exactly one of `in`, `at` or `every`"}, status=400)
        title = str(body.get("title") or "").strip()
        prompt = str(body.get("prompt") or body.get("spec") or title).strip()
        if not title or not prompt:
            return web.json_response({"error": "title and prompt are required"}, status=400)
        action = str(body.get("action") or "check").strip().lower()
        if action not in ("task", "check"):
            return web.json_response({"error": "action must be task or check"}, status=400)
        mode = str(body.get("mode") or "ASSIST").strip().upper()
        timezone_name = str(body.get("timezone") or "").strip() or None
        try:
            if every:
                timer = self.db.add_timer(
                    kind="schedule",
                    action=action,
                    title=title,
                    prompt_spec=prompt,
                    cron_expr=every,
                    timezone_name=timezone_name,
                    repo=str(body.get("repo") or "").strip() or None,
                    mode=mode,
                    verify_cmd=str(body.get("verify") or "").strip() or None,
                    max_cost_usd=(
                        float(body["max_cost_usd"])
                        if body.get("max_cost_usd") is not None
                        else None
                    ),
                    created_by="owner",
                )
            else:
                now = datetime.now(timezone.utc)
                if in_text:
                    run_at = now + schedule.parse_duration(in_text)
                else:
                    run_at = schedule.next_at(at_text, timezone_name, now=now)
                timer = self.db.add_timer(
                    kind="followup",
                    action=action,
                    title=title,
                    prompt_spec=prompt,
                    run_at=run_at,
                    timezone_name=timezone_name,
                    repo=str(body.get("repo") or "").strip() or None,
                    mode=mode,
                    verify_cmd=str(body.get("verify") or "").strip() or None,
                    max_cost_usd=(
                        float(body["max_cost_usd"])
                        if body.get("max_cost_usd") is not None
                        else None
                    ),
                    created_by="owner",
                )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        self.reconciler.wake()
        return web.json_response({"timer": self._timer_dict(timer)})

    async def handle_cancel_timer(self, request: web.Request) -> web.Response:
        timer = self.db.cancel_timer(request.match_info["timer_id"])
        if timer is None:
            return web.json_response({"error": "Timer not found"}, status=404)
        return web.json_response({"ok": True, "timer": self._timer_dict(timer)})

    async def handle_approval(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        rec = self.runs.get(run_id)
        if rec is None:
            return web.json_response({"error": "Run not found"}, status=404)

        body = await self._read_json_dict(request)
        approved = str(body.get("choice") or "").lower() == "once"

        if rec.pending_approval_future and not rec.pending_approval_future.done():
            rec.pending_approval_future.set_result("yes" if approved else "no")
            rec.publish({"event": "approval.responded", "run_id": run_id, "approved": approved})
            self.hub.emit(
                EVENT_APPROVAL_RESPONDED,
                run_id=run_id,
                approved=approved,
                source="voice",
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
        await self._stop_run(rec, reason="Stopped by the user.")
        return web.json_response({"ok": True})

    async def _stop_run(self, rec: RunRecord, *, reason: str, replaced: bool = False) -> None:
        """Stop one run: abort its worker, release a waiting question, and cancel its task."""
        if not replaced:
            self.traces.mark_stopped(rec.trace_id)
        self.reconciler.abort_run(rec.run_id)
        for runner in self.runners.values():
            try:
                await runner.interrupt(rec.run_id)
            except Exception:
                pass
        if rec.pending_approval_future and not rec.pending_approval_future.done():
            rec.pending_approval_future.set_result(None)
        if rec.task_handle and not rec.task_handle.done() and rec.task_handle is not asyncio.current_task():
            rec.task_handle.cancel()
        rec.status = "cancelled"
        rec.publish({"event": "run.cancelled", "run_id": rec.run_id, "reason": reason})
        self.hub.emit(EVENT_RUN_STATUS, run_id=rec.run_id, status="cancelled", source="voice")

    async def handle_ask(self, request: web.Request) -> web.Response:
        """Synchronous iPhone Shortcut / Siri endpoint (`POST /v1/ask` -> `{"spoken": "..."}`)."""
        body = await self._read_json_dict(request)
        user_input = str(body.get("input") or "").strip()
        if not user_input:
            return web.json_response({"error": "input is required"}, status=400)

        self._prune_history()
        run_id = f"ask-{uuid.uuid4().hex[:12]}"
        rec = RunRecord(
            run_id=run_id,
            session_id=run_id,
            trace_id=uuid.uuid4().hex,
            turn_id=f"ask-{run_id[-6:]}",
        )
        self.runs[run_id] = rec
        self.traces.note_run_created(
            trace_id=rec.trace_id,
            turn_id=rec.turn_id,
            session_id=run_id,
            user_input=user_input,
            project="",
            runner="",
            replace=False,
        )
        self.hub.emit(
            EVENT_HTTP_INBOUND,
            source="iphone_ask",
            route="/v1/ask",
            run_id=run_id,
            session_id=run_id,
            trace_id=rec.trace_id,
            turn_id=rec.turn_id,
            input_preview=preview(user_input),
        )
        await self._execute_run(rec, user_input, allow_live_approval=False)
        spoken = rec.output or rec.error or "Done."
        return web.json_response({"run_id": run_id, "status": rec.status, "spoken": spoken})

    def _reminder_from_text(self, text: str) -> str | None:
        """Deterministic reminders and scheduled checks; no model call, no router prompt change."""
        recurring = schedule.parse_recurring_phrase(text)
        if recurring is not None:
            cron, tz_name, body = recurring
            title = (body or "Scheduled check")[:80]
            try:
                timer = self.db.add_timer(
                    kind="schedule",
                    action="check",
                    title=title,
                    prompt_spec=body or text,
                    cron_expr=cron,
                    timezone_name=tz_name,
                )
            except ValueError as exc:
                return f"I couldn't schedule that: {exc}."
            self.reconciler.wake()
            when = schedule.parse_utc(timer.next_run_at).astimezone(
                schedule.resolve_timezone(timer.timezone)[1]
            )
            description = schedule.timer_description(
                kind="schedule", cron_expr=cron, timezone_name=timer.timezone, next_run_at=when
            )
            return f"Okay. {description}. I'll {body or 'check'}."

        match = _REMIND_RE.match(text) or _CHECK_IN_RE.match(text)
        if match is None:
            return None
        groups = match.groupdict()
        body = (groups.get("body") or groups.get("body2") or "").strip(" .")
        try:
            delta = schedule.parse_duration(match.group("duration"))
            run_at = datetime.now(timezone.utc) + delta
            timer = self.db.add_timer(
                kind="followup",
                action="check",
                title=(body or "Scheduled check")[:80],
                prompt_spec=body or text.strip(),
                run_at=run_at,
            )
        except ValueError as exc:
            return f"I couldn't schedule that: {exc}."
        self.reconciler.wake()
        local = schedule.parse_utc(timer.next_run_at).astimezone(
            schedule.resolve_timezone(timer.timezone)[1]
        )
        about = f" about {body}" if body else ""
        return (
            f"Okay. I'll check back in {schedule.describe_duration(delta)}{about}, "
            f"at {local.strftime('%H:%M')}."
        )

    async def _execute_run(
        self, rec: RunRecord, user_input: str, *, allow_live_approval: bool = True,
        screenshot: str | None = None,
    ) -> None:
        """Wraps the run body with live telemetry bookkeeping for the dashboard."""
        self.hub.emit(
            EVENT_RUN_STATUS,
            run_id=rec.run_id,
            session_id=rec.session_id,
            trace_id=rec.trace_id,
            turn_id=rec.turn_id,
            status="running",
            input_preview=preview(user_input),
        )
        try:
            await self._execute_run_inner(
                rec, user_input, allow_live_approval=allow_live_approval, screenshot=screenshot
            )
        finally:
            self.hub.emit(
                EVENT_RUN_STATUS,
                run_id=rec.run_id,
                session_id=rec.session_id,
                trace_id=rec.trace_id,
                turn_id=rec.turn_id,
                status=rec.status,
                output_preview=preview(rec.output),
                error=preview(rec.error),
            )
            self.hub.emit(
                EVENT_HTTP_COMPLETED,
                run_id=rec.run_id,
                session_id=rec.session_id,
                trace_id=rec.trace_id,
                turn_id=rec.turn_id,
                status=rec.status,
                spoken=preview(rec.output or rec.error),
            )

    async def _execute_run_inner(
        self, rec: RunRecord, user_input: str, *, allow_live_approval: bool = True,
        screenshot: str | None = None,
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
                self._announce_queued_outbox()
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
                for item in reports:
                    # The same line every other path writes, so a reconnecting voice does not hear it again.
                    memory.append_reported(self.cfg.home_dir, f"[{item.kind}] {item.summary_spoken}")
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
                    fut: asyncio.Future[str | None] = loop.create_future()
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
                        answer = await asyncio.wait_for(fut, timeout=120.0)
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
                    if answer is None:
                        # Stopped with no answer: the approval is still the owner's to decide, so it stays queued.
                        rec.output = f"{summary_text} No answer; the approval stays queued."
                        rec.status = "completed"
                        rec.publish(
                            {"event": "run.completed", "run_id": rec.run_id, "output": rec.output}
                        )
                        return
                    approved = is_approval(answer)

                    outbox_row = self.db.resolve_approval(pending_approval.id, approved)
                    try:
                        ob_details = (
                            json.loads(outbox_row.details_json or "{}") if outbox_row else {}
                        )
                    except ValueError:
                        ob_details = {}
                    if (
                        approved
                        and outbox_row is not None
                        and isinstance(ob_details, dict)
                        and ob_details.get("rollback_sha")
                    ):
                        self.db.mark_outbox_consumed(outbox_row.id)
                        raw_pid = ob_details.get("proposal_id")
                        proposal_id = int(raw_pid) if raw_pid is not None else None
                        rollback_sha = str(ob_details["rollback_sha"])
                        rolled_back = await self.reconciler.apply_rollback(
                            rollback_sha, proposal_id=proposal_id
                        )
                        rec.output = (
                            f"Rolled back to {rollback_sha[:7]}."
                            if rolled_back
                            else f"Rollback to {rollback_sha[:7]} failed."
                        )
                        rec.status = "completed"
                        rec.publish(
                            {"event": "run.completed", "run_id": rec.run_id, "output": rec.output}
                        )
                        self._schedule_restart_if_pending()
                        return
                    if (
                        outbox_row is not None
                        and isinstance(ob_details, dict)
                        and ob_details.get("runner")
                        and ob_details.get("session_id")
                    ):
                        await self._forward_runner_outbox(outbox_row, ob_details, answer)
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
                            approval_cb=self._make_bool_approval_cb(rec),
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

            reminder_reply = self._reminder_from_text(user_input)
            if reminder_reply:
                rec.output = reminder_reply
                rec.status = "completed"
                rec.publish({"event": "run.completed", "run_id": rec.run_id, "output": rec.output})
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
                    approval_cb=(self._make_bool_approval_cb(rec) if allow_live_approval else None),
                )
                restart_note = " Restarting Brain daemon now." if self.reconciler.restart_pending else ""
                rec.output = f"Task {finished.id} finished with status {finished.status}.{restart_note}"
                rec.status = "completed"
                rec.publish({"event": "run.completed", "run_id": rec.run_id, "output": rec.output})
                self._schedule_restart_if_pending()
                return

            # 2. One router decision: one-shot pi answer, a T3/Claude/OpenCode thread, or the board.
            async def emit(event: dict[str, Any]) -> None:
                rec.publish({**event, "run_id": rec.run_id})

            result = await self.orchestrator.route(
                run_id=rec.run_id,
                user_input=user_input,
                session_id=rec.session_id,
                pinned=rec.pinned,
                approval_cb=(self._make_approval_cb(rec) if allow_live_approval else None),
                emit=emit,
                screenshot=screenshot,
                session_mode=rec.session_mode,
                cwd=rec.cwd,
                chat_session_key=rec.chat_session_key,
                trace_id=rec.trace_id,
                turn_id=rec.turn_id,
            )
            if result.project:
                self._last_project_by_session[rec.session_id] = result.project
                while len(self._last_project_by_session) > 200:
                    self._last_project_by_session.pop(next(iter(self._last_project_by_session)))
            if result.outcome == "deferred_approval" and result.question:
                # Nobody can answer live (an iPhone one-shot, or an unanswered prompt): leave it on the board.
                details = {
                    **result.question,
                    "runner": result.runner or "t3",
                    "session_id": result.session_id or "",
                }
                outbox = self.db.queue_outbox(
                    kind="approval_request",
                    summary_spoken=str(result.question.get("description") or result.summary),
                    details=details,
                )
                result.summary = f"{result.summary} (queued as {outbox.id})"
            if result.outcome in ("errored", "timed_out"):
                rec.error = result.summary
                rec.status = "failed"
                rec.publish({"event": "run.failed", "run_id": rec.run_id, "error": rec.error})
                return
            notes: list[str] = []
            if self.reconciler.restart_pending:
                notes.append("Restarting Brain daemon now.")
            rec.output = " ".join([result.summary, *notes]).strip()
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
        ) -> str | None:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[str | None] = loop.create_future()
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
                source="voice",
            )
            try:
                return await asyncio.wait_for(fut, timeout=120.0)
            except asyncio.TimeoutError:
                return None
            finally:
                rec.pending_approval_future = None

        return _cb

    def _make_bool_approval_cb(self, rec: RunRecord) -> Any:
        """The reconciler asks a yes/no question; the voice answers in words, so map them here."""
        from jarvis_core.questions import is_approval

        text_cb = self._make_approval_cb(rec)

        async def _cb(summary_spoken: str, details: str, session_id: str | None) -> bool | None:
            answer = await text_cb(summary_spoken, details, session_id)
            return None if answer is None else is_approval(answer)

        return _cb
