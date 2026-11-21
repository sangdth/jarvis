from __future__ import annotations

import asyncio
import hmac
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

from superbrain.config import Settings, settings
from superbrain.db import BrainDB
from superbrain.reconciler import Reconciler

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
    Serves the 5-endpoint `HermesAdapter` HTTP/SSE contract plus `POST /v1/ask` for iPhone Shortcuts.
    """

    def __init__(
        self, db: BrainDB, reconciler: Reconciler, cfg: Settings = settings
    ) -> None:
        self.db = db
        self.reconciler = reconciler
        self.cfg = cfg
        self.runs: dict[str, RunRecord] = {}
        self._pi_sessions: dict[str, str] = {}
        self.app = web.Application(middlewares=[self._auth_middleware])
        self.app.add_routes(
            [
                web.post("/v1/runs", self.handle_create_run),
                web.get("/v1/runs/{run_id}", self.handle_get_run),
                web.get("/v1/runs/{run_id}/events", self.handle_stream_events),
                web.post("/v1/runs/{run_id}/approval", self.handle_approval),
                web.post("/v1/runs/{run_id}/stop", self.handle_stop_run),
                web.post("/v1/ask", self.handle_ask),
            ]
        )

    @web.middleware
    async def _auth_middleware(
        self,
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        expected = self.cfg.superbrain_api_key
        if expected:
            auth = request.headers.get("Authorization", "")
            token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
            if not hmac.compare_digest(token, expected):
                return web.json_response({"error": "Unauthorized"}, status=401)
        return await handler(request)

    async def handle_create_run(self, request: web.Request) -> web.Response:
        body = await request.json()
        user_input = str(body.get("input") or "").strip()
        if not user_input:
            return web.json_response({"error": "input is required"}, status=400)

        run_id = f"run-{uuid.uuid4().hex[:12]}"
        session_id = str(body.get("session_id") or run_id)
        rec = RunRecord(run_id=run_id, session_id=session_id)
        self.runs[run_id] = rec
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

    async def handle_approval(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        rec = self.runs.get(run_id)
        if rec is None:
            return web.json_response({"error": "Run not found"}, status=404)

        body = await request.json()
        approved = str(body.get("choice") or "").lower() == "once"

        if rec.pending_outbox_id:
            outbox_row = self.db.resolve_approval(rec.pending_outbox_id, approved)
            rec.pending_outbox_id = None
            rec.publish({"event": "approval.responded", "run_id": run_id, "approved": approved})
            state_word = "Approved" if approved else "Rejected"
            task_note = f" task {outbox_row.task_id}" if outbox_row and outbox_row.task_id else ""
            rec.output = f"{state_word}{task_note}."
            rec.status = "completed"
            rec.publish({"event": "run.completed", "run_id": run_id, "output": rec.output})
            return web.json_response({"ok": True})

        if rec.pending_approval_future and not rec.pending_approval_future.done():
            rec.pending_approval_future.set_result(approved)
            rec.publish({"event": "approval.responded", "run_id": run_id, "approved": approved})
            return web.json_response({"ok": True})

        return web.json_response({"error": "No pending approval on this run"}, status=409)

    async def handle_stop_run(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        rec = self.runs.get(run_id)
        if rec is None:
            return web.json_response({"error": "Run not found"}, status=404)

        self.reconciler.abort_run(run_id)
        if rec.pending_approval_future and not rec.pending_approval_future.done():
            rec.pending_approval_future.set_result(False)
        if rec.task_handle and not rec.task_handle.done():
            rec.task_handle.cancel()
        rec.status = "cancelled"
        rec.publish({"event": "run.cancelled", "run_id": run_id})
        return web.json_response({"ok": True})

    async def handle_ask(self, request: web.Request) -> web.Response:
        """Synchronous iPhone Shortcut / Siri endpoint (`POST /v1/ask` -> `{"spoken": "..."}`)."""
        body = await request.json()
        user_input = str(body.get("input") or "").strip()
        if not user_input:
            return web.json_response({"error": "input is required"}, status=400)

        run_id = f"ask-{uuid.uuid4().hex[:12]}"
        rec = RunRecord(run_id=run_id, session_id=run_id)
        self.runs[run_id] = rec
        await self._execute_run(rec, user_input, allow_live_approval=False)
        spoken = rec.output or rec.error or "Done."
        return web.json_response({"run_id": run_id, "status": rec.status, "spoken": spoken})

    async def _execute_run(
        self, rec: RunRecord, user_input: str, *, allow_live_approval: bool = True
    ) -> None:
        try:
            # 1. Fast zero-token commands (mode switches, morning brief, explicit task trigger)
            if _AUTONOMOUS_RE.search(user_input):
                st = self.db.set_mode("AUTONOMOUS")
                ready_count = len(self.db.list_tasks(status="ready"))
                rec.output = (
                    f"Switched to autonomous mode. {ready_count} ready task"
                    f"{'' if ready_count == 1 else 's'} queued, "
                    f"${st.budget_remaining_usd:.2f} budget remaining today."
                )
                rec.status = "completed"
                rec.publish({"event": "run.completed", "run_id": rec.run_id, "output": rec.output})
                return

            if _ASSIST_RE.search(user_input):
                self.db.set_mode("ASSIST")
                rec.output = "Switched to assist mode. Background autonomous runs are paused."
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
                    rec.pending_outbox_id = pending_approval.id
                    rec.publish(
                        {
                            "event": "approval.request",
                            "run_id": rec.run_id,
                            "request_id": pending_approval.id,
                            "description": f"{summary_text} Next approval needed: {pending_approval.summary_spoken}",
                        }
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
                rec.output = f"Task {finished.id} finished with status {finished.status}."
                rec.status = "completed"
                rec.publish({"event": "run.completed", "run_id": rec.run_id, "output": rec.output})
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
            else:
                rec.output = res.summary
                rec.status = "completed"
                rec.publish({"event": "run.completed", "run_id": rec.run_id, "output": rec.output})
        except asyncio.CancelledError:
            rec.status = "cancelled"
            rec.publish({"event": "run.cancelled", "run_id": rec.run_id})
        except Exception as exc:
            rec.error = str(exc)
            rec.status = "failed"
            rec.publish({"event": "run.failed", "run_id": rec.run_id, "error": rec.error})

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
            try:
                return await asyncio.wait_for(fut, timeout=120.0)
            except asyncio.TimeoutError:
                return None
            finally:
                rec.pending_approval_future = None

        return _cb
