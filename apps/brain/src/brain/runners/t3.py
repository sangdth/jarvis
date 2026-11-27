"""T3 Code as a runner: long-lived, human-friendly project threads the owner can watch and take over.

The brain talks to T3's app server over the same WebSocket the T3 window uses (Effect RPC, protocol 2).
One connection carries every watched thread, so two projects can work at once; the thread bound to a repo is
remembered in `state.db` (`threads`), so the next task continues the same conversation instead of opening a
new one. If nothing answers on loopback, the brain starts `t3 serve --mode desktop` itself.

Questions and approvals are surfaced verbatim (`question.request` / `approval.request`); the answer is mapped
back deterministically by `jarvis_core.questions`. No model ever decides an approval.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
import signal
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus

from brain import memory
from brain.config import Settings
from brain.db import BrainDB
from brain.runners.base import EventCallback, RunRequest, RunResult
from brain.sandbox import strip_gateway_secrets
from jarvis_core.projects import resolve_project
from jarvis_core.questions import describe_fields, form_answer, is_approval, question_fields
from jarvis_core.sessions import AgentSession, match_sessions
from jarvis_core.t3 import ACTIVE_STATUSES, T3_HOME, current_thread, find_project, recent_threads, t3_defaults

PROTOCOL = 2  # T3's ORCHESTRATION_PROTOCOL_VERSION; the server answers 426 to any other
_ACTIVE = ACTIVE_STATUSES
_ASKS = {"approval_request", "user_input_request"}
SYNC_MASTER = "Sync with the master branch first. Then: "


@dataclass
class _ThreadState:
    thread_id: str
    sequence: int | None = None
    runs: dict[str, str] = field(default_factory=dict)
    last_text: dict[str, str] = field(default_factory=dict)
    pending: dict[str, Any] | None = None
    listeners: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    title: str = ""
    cwd: str = ""
    branch: str = ""
    worktree_path: str | None = None
    project_id: str | None = None
    # A thread this runner just launched already carries the task as its first message.
    initial_message_sent: bool = False
    watched: bool = False
    on_unsolicited: Callable[[dict[str, Any]], None] | None = None

    def publish(self, event: dict[str, Any]) -> None:
        if self.listeners:
            for queue in list(self.listeners):
                queue.put_nowait(event)
        elif self.watched and self.on_unsolicited is not None:
            self.on_unsolicited({**event, "runner": "t3", "session_id": self.thread_id})


class T3Runner:
    """Drives T3 Code over its orchestration WebSocket; starts a headless server when nothing answers."""

    id = "t3"

    def __init__(self, cfg: Settings, db: BrainDB) -> None:
        self.cfg = cfg
        self.db = db
        base = (cfg.t3_url or "http://127.0.0.1:3773").rstrip("/")
        self.ws_url = base.replace("http", "ws", 1) + f"/ws?orchestrationProtocol={PROTOCOL}"
        self._base = base
        self._ws: ClientConnection | None = None
        self._token: str | None = None
        self._ids = itertools.count(1)
        self._calls: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._snapshots: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._subs: dict[str, str] = {}  # subscription request id -> thread id
        self._threads: dict[str, _ThreadState] = {}
        self._active_runs: dict[str, tuple[str, str | None]] = {}  # server run id -> (thread id, t3 run id)
        self.on_unsolicited: Callable[[dict[str, Any]], None] | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._autostart_lock = asyncio.Lock()
        self._spawned_proc: asyncio.subprocess.Process | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._run_loop(), name="t3-runner")

    async def stop(self) -> None:
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        if self._spawned_proc is not None and self._spawned_proc.returncode is None:
            try:
                os.killpg(os.getpgid(self._spawned_proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                try:
                    self._spawned_proc.terminate()
                except ProcessLookupError:
                    pass

    async def _run_loop(self) -> None:
        while True:
            wait = 5.0
            try:
                self._token = self._token or await self._issue_token()
                async with self._connect() as ws:
                    self._ws = ws
                    self._connected.set()
                    logger.info(f"[t3] Connected to T3 Code at {self.ws_url}")
                    for state in self._threads.values():
                        await self._subscribe(state)
                    async for raw in ws:
                        await self._on_message(ws, json.loads(raw))
            except asyncio.CancelledError:
                raise
            except InvalidStatus as exc:
                code = exc.response.status_code
                if code == 426:
                    logger.error(f"[t3] T3 Code changed its protocol (brain speaks {PROTOCOL}); update the runner.")
                    wait = 300.0
                elif code in (401, 403):
                    self._token = None
                logger.debug(f"[t3] T3 Code refused the connection ({code}); retrying.")
            except Exception as exc:
                logger.debug(f"[t3] T3 Code unavailable ({exc!r}); retrying in {wait:.0f}s.")
                await self._maybe_autostart()
            finally:
                self._ws = None
                self._connected.clear()
                self._subs.clear()
                for call in (*self._calls.values(), *self._snapshots.values()):
                    call.cancel()
                self._calls.clear()
                self._snapshots.clear()
                for state in self._threads.values():
                    state.runs.clear()
                    state.last_text.clear()
                    state.pending = None
            await asyncio.sleep(wait)

    async def _ensure_connected(self) -> bool:
        await self.start()
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=self.cfg.t3_start_timeout_seconds + 2)
        except asyncio.TimeoutError:
            return False
        return self._ws is not None

    async def _issue_token(self) -> str:
        t3 = shutil.which("t3") or str(T3_HOME / "bin" / "t3")
        proc = await asyncio.create_subprocess_exec(
            t3, "auth", "session", "issue", "--scope", "orchestration:read", "--scope", "orchestration:operate",
            "--ttl", "30d", "--label", "jarvis brain", "--token-only",
            env=strip_gateway_secrets(dict(os.environ)),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await proc.communicate()
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        if proc.returncode != 0:
            raise RuntimeError(f"`t3 auth session issue` failed: {err.decode().strip()}")
        return out.decode().strip()

    def _connect(self) -> Any:
        return connect(self.ws_url, additional_headers={"Authorization": f"Bearer {self._token}"}, max_size=None)

    async def _maybe_autostart(self) -> None:
        """Start `t3 serve` when nothing answers on loopback, at most once per failed connection cycle."""
        if not self.cfg.t3_auto_start:
            return
        host = self._host()
        if host not in ("127.0.0.1", "localhost", "::1"):
            return  # a remote T3 is the owner's to keep running
        if self._spawned_proc is not None and self._spawned_proc.returncode is None:
            return
        async with self._autostart_lock:
            if self._spawned_proc is not None and self._spawned_proc.returncode is None:
                return
            t3 = shutil.which("t3") or str(Path.home() / ".local" / "bin" / "t3")
            if not Path(t3).exists() and shutil.which("t3") is None:
                logger.warning("[t3] T3 Code is not running and the `t3` CLI is not on PATH.")
                return
            log = self.cfg.home_dir / "logs"
            log.mkdir(parents=True, exist_ok=True, mode=0o700)
            log_file = log / "t3-serve.log"
            logger.warning(f"[t3] Nothing answers on {self._base}; starting `t3 serve` (log: {log_file}).")
            with log_file.open("ab") as handle:
                self._spawned_proc = await asyncio.create_subprocess_exec(
                    t3, "serve", "--mode", "desktop", "--host", host, "--port", str(self._port()),
                    env=strip_gateway_secrets(dict(os.environ)),
                    stdout=handle, stderr=handle, start_new_session=True,
                )
            deadline = asyncio.get_running_loop().time() + self.cfg.t3_start_timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
                try:
                    _, writer = await asyncio.open_connection(host, self._port())
                except OSError:
                    await asyncio.sleep(0.2)
                    continue
                writer.close()
                await writer.wait_closed()
                self._token = None  # the new server has its own auth store
                return

    def _host(self) -> str:
        from urllib.parse import urlparse

        return urlparse(self._base).hostname or "127.0.0.1"

    def _port(self) -> int:
        from urllib.parse import urlparse

        return urlparse(self._base).port or 3773

    # ------------------------------------------------------------------ protocol

    async def _on_message(self, ws: ClientConnection, msg: dict[str, Any]) -> None:
        tag = msg.get("_tag")
        if tag == "Ping":
            await ws.send(json.dumps({"_tag": "Pong"}))
        elif tag == "Chunk":
            await ws.send(json.dumps({"_tag": "Ack", "requestId": msg["requestId"]}))  # no Ack, no next chunk
            thread_id = self._subs.get(msg["requestId"])
            if thread_id is not None:
                state = self._state(thread_id)
                for item in msg.get("values", []):
                    await self._on_stream_item(state, item)
            elif (snap := self._snapshots.get(msg["requestId"])) is not None:
                for item in msg.get("values", []):
                    if item.get("kind") == "snapshot" and not snap.done():
                        snap.set_result(item["snapshot"])
        elif tag == "Exit":
            if msg["requestId"] in self._subs:
                thread_id = self._subs.pop(msg["requestId"], None)
                exit_tag = (msg.get("exit") or {}).get("_tag")
                if exit_tag == "Failure":
                    if thread_id:
                        self._threads.pop(thread_id, None)
                    logger.warning(f"[t3] Thread subscription {thread_id} failed; stopped watching it.")
                else:
                    thread_state = self._threads.get(thread_id or "")
                    if thread_state is not None and self._ws is not None:
                        asyncio.create_task(self._resubscribe_later(thread_state))
            if (snap := self._snapshots.pop(msg["requestId"], None)) is not None and not snap.done():
                snap.set_exception(RuntimeError(f"T3 shell subscription failed: {json.dumps(msg)[:300]}"))
            call = self._calls.pop(msg["requestId"], None)
            if call is not None and not call.done():
                call.set_result(msg["exit"])

    async def _request(self, method: str, payload: dict[str, Any]) -> str:
        if self._ws is None:
            raise ConnectionError("T3 Code is not connected")
        request_id = str(next(self._ids))
        await self._ws.send(
            json.dumps({"_tag": "Request", "id": request_id, "tag": method, "payload": payload, "headers": []})
        )
        return request_id

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            request_id = await self._request(method, payload)
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._calls[request_id] = future
            exit_ = await asyncio.wait_for(future, 30.0)
        except (ConnectionError, OSError, asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError) and (task := asyncio.current_task()) and task.cancelling():
                raise
            logger.error(f"[t3] {method} got no answer ({exc!r})")
            return None
        if exit_.get("_tag") != "Success":
            logger.error(f"[t3] {method} failed: {json.dumps(exit_)[:400]}")
            return None
        return exit_.get("value") or {}

    async def _command(self, thread_id: str, command: dict[str, Any]) -> bool:
        payload = {"commandId": str(uuid.uuid4()), "threadId": thread_id, **command}
        return await self._call("orchestration.dispatchCommand", payload) is not None

    async def _resubscribe_later(self, state: _ThreadState) -> None:
        await asyncio.sleep(1.0)
        if self._ws is not None and self._threads.get(state.thread_id) is state:
            await self._subscribe(state)

    async def _subscribe(self, state: _ThreadState) -> None:
        if self._ws is None:
            return
        payload: dict[str, Any] = {"threadId": state.thread_id}
        if state.sequence is not None:
            payload["afterSequence"] = state.sequence
        try:
            request_id = await self._request("orchestration.subscribeThread", payload)
        except ConnectionError:
            return
        self._subs[request_id] = state.thread_id

    async def shell_snapshot(self) -> dict[str, Any]:
        """T3's projects and threads, read over the open connection or a short-lived one."""
        if self._ws is not None:
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            request_id = str(next(self._ids))
            self._snapshots[request_id] = future
            try:
                await self._ws.send(json.dumps({
                    "_tag": "Request", "id": request_id, "tag": "orchestration.subscribeShell",
                    "payload": {}, "headers": [],
                }))
                return await asyncio.wait_for(future, 10.0)
            finally:
                self._snapshots.pop(request_id, None)
                if self._ws is not None:
                    try:
                        await self._ws.send(json.dumps({"_tag": "Interrupt", "id": request_id}))
                    except OSError:
                        pass
        self._token = self._token or await self._issue_token()
        async with asyncio.timeout(10.0), self._connect() as ws:
            request = {"_tag": "Request", "id": "1", "tag": "orchestration.subscribeShell",
                       "payload": {}, "headers": []}
            await ws.send(json.dumps(request))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("_tag") == "Ping":
                    await ws.send(json.dumps({"_tag": "Pong"}))
                elif msg.get("_tag") == "Exit":
                    raise RuntimeError(f"T3 shell subscription failed: {json.dumps(msg)[:300]}")
                for item in msg.get("values", []) if msg.get("_tag") == "Chunk" else []:
                    if item.get("kind") == "snapshot":
                        return item["snapshot"]
        raise RuntimeError("T3 shell subscription closed before its snapshot")

    # ------------------------------------------------------------------ events

    def _state(self, thread_id: str) -> _ThreadState:
        state = self._threads.get(thread_id)
        if state is None:
            state = _ThreadState(thread_id=thread_id, on_unsolicited=self.on_unsolicited)
            self._threads[thread_id] = state
        else:
            state.on_unsolicited = self.on_unsolicited
        return state

    async def _on_stream_item(self, state: _ThreadState, item: dict[str, Any]) -> None:
        if item.get("kind") == "snapshot":
            state.sequence = item.get("snapshotSequence")
            projection = item.get("projection") or {}
            state.runs = {r["id"]: r["status"] for r in projection.get("runs", []) if r.get("status") in _ACTIVE}
            for turn_item in projection.get("turnItems", []):
                await self._on_turn_item(state, turn_item)
        elif item.get("kind") == "event":
            state.sequence = max(state.sequence or 0, item.get("sequence") or 0)
            await self._on_event(state, item["event"])

    async def _on_event(self, state: _ThreadState, event: dict[str, Any]) -> None:
        kind, payload = event.get("type"), event.get("payload") or {}
        if kind == "turn-item.updated":
            await self._on_turn_item(state, payload)
        elif kind == "runtime-request.updated" and payload.get("status") != "pending":
            self._settle(state, payload.get("id"))
        elif kind == "run.updated":
            run_id, status = payload.get("id") or event.get("runId"), payload.get("status")
            if not run_id:
                return
            if status in _ACTIVE:
                state.runs[run_id] = str(status)
                return
            state.runs.pop(run_id, None)
            text = state.last_text.pop(run_id, "")
            state.pending = None
            if status == "completed":
                state.publish({"event": "turn.completed", "run_id": run_id, "text": text})
            elif status == "failed":
                state.publish({"event": "turn.failed", "run_id": run_id, "error": text or "The turn failed."})
            else:
                state.publish({"event": "turn.cancelled", "run_id": run_id})

    async def _on_turn_item(self, state: _ThreadState, item: dict[str, Any]) -> None:
        kind = item.get("type")
        if kind == "assistant_message" and item.get("status") == "completed" and item.get("runId") in state.runs:
            state.last_text[item["runId"]] = item.get("text") or ""
        elif kind in _ASKS:
            if item.get("status") != "waiting":
                self._settle(state, item.get("requestId"))
                return
            state.pending = item
            if kind == "approval_request":
                what = item.get("prompt") or item.get("requestKind") or "a tool call"
                reason = f". {item['reason']}" if item.get("reason") else ""
                state.publish({
                    "event": "approval.request", "request_id": item.get("requestId"),
                    "description": f"needs approval: {what}{reason}",
                })
            else:
                fields = question_fields(item.get("questions", []))
                state.publish({
                    "event": "question.request", "request_id": item.get("requestId"),
                    "description": f"asks: {describe_fields(fields)}",
                    "questions": fields,
                })

    @staticmethod
    def _settle(state: _ThreadState, request_id: str | None) -> None:
        if state.pending is not None and state.pending.get("requestId") == request_id:
            state.pending = None
            state.publish({"event": "approval.responded", "request_id": request_id})

    # ------------------------------------------------------------------ sessions

    async def list_sessions(self, cwd: str | None = None, limit: int = 15) -> list[AgentSession]:
        snapshot = await self.shell_snapshot()
        project = find_project(snapshot.get("projects", []), cwd) if cwd else None
        if cwd and project is None:
            return []
        return [
            AgentSession(
                id=t["id"], title=t["title"], cwd=t["root"], branch=t["branch"],
                running=t["status"] in _ACTIVE,
                updated_at=datetime.fromisoformat(t["updatedAt"]).timestamp() if t["updatedAt"] else 0.0,
                visited_at=datetime.fromisoformat(t["visitedAt"]).timestamp() if t.get("visitedAt") else 0.0,
            )
            for t in recent_threads(snapshot, limit=None)
            if project is None or t["projectId"] == project["id"]
        ][:limit]

    async def resolve_folder_session(self, project_root: str, worktree_path: str | None) -> AgentSession | None:
        snapshot = await self.shell_snapshot()
        t = current_thread(snapshot, project_root, worktree_path)
        if t is None:
            return None
        return AgentSession(
            id=t["id"], title=t["title"], cwd=t["root"], branch=t["branch"],
            running=t["status"] in _ACTIVE,
            updated_at=datetime.fromisoformat(t["updatedAt"]).timestamp() if t["updatedAt"] else 0.0,
            visited_at=datetime.fromisoformat(t["visitedAt"]).timestamp() if t.get("visitedAt") else 0.0,
        )

    async def watch_session(self, session_id: str) -> dict[str, Any] | None:
        """Subscribe to a `jarvis connect` thread immediately and return any prompt it is already waiting on."""
        await self._ensure_connected()
        state = self._threads.get(session_id) or (await self._restore_state(session_id)) or self._state(session_id)
        state.watched = True
        state.on_unsolicited = self.on_unsolicited
        if not any(tid == session_id for tid in self._subs.values()):
            await self._subscribe(state)
        if state.pending is None:
            return None
        pending = state.pending
        if pending.get("type") == "approval_request":
            return {
                "event": "approval.request",
                "request_id": pending.get("requestId"),
                "description": f"needs approval: {pending.get('prompt') or pending.get('requestKind') or 'a tool call'}",
            }
        fields = question_fields(pending.get("questions", []))
        return {
            "event": "question.request",
            "request_id": pending.get("requestId"),
            "description": f"asks: {describe_fields(fields)}",
            "questions": fields,
        }

    # ------------------------------------------------------------------ work

    async def run(self, request: RunRequest, emit: EventCallback) -> RunResult:
        if not await self._ensure_connected():
            return RunResult(outcome="errored", summary="T3 Code is not running and could not be started.")
        cwd = self._cwd(request)
        if cwd is None:
            return RunResult(outcome="errored", summary=f"No listed project matches {request.project!r}.")
        repo_key = str(cwd)
        state, launched, error = await self._resolve_thread(request, cwd, repo_key)
        if state is None:
            return RunResult(outcome="errored", summary=error or "Could not open a T3 thread.")
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        state.listeners.add(queue)
        if state.pending is not None:
            # A question asked before this run started: surface it again so the new listener can answer it.
            pending = state.pending
            if pending.get("type") == "approval_request":
                queue.put_nowait({"event": "approval.request", "request_id": pending.get("requestId"),
                                  "description": pending.get("prompt") or "needs approval"})
            else:
                fields = question_fields(pending.get("questions", []))
                queue.put_nowait({"event": "question.request", "request_id": pending.get("requestId"),
                                  "description": f"asks: {describe_fields(fields)}", "questions": fields})
        try:
            before = set(state.runs)
            self._active_runs[request.run_id] = (state.thread_id, None)
            # A thread this run just launched carries the task as its first message already; dispatching it
            # again would run the whole thing a second time.
            already_sent = state.initial_message_sent
            state.initial_message_sent = False
            ok = already_sent or await self._command(state.thread_id, {
                "type": "message.dispatch", "createdBy": "user", "creationSource": "web",
                "messageId": str(uuid.uuid4()), "text": request.prompt, "attachments": [],
                "deliveryIntent": "auto", "dispatchMode": {"type": "queue_after_active"},
            })
            if not ok:
                return RunResult(outcome="errored", summary="T3 Code did not accept the message.",
                                 session_id=state.thread_id, session_title=state.title)
            if launched:
                await emit({"event": "session.switched", "runner": self.id,
                            "session_id": state.thread_id, "title": state.title,
                            "project": Path(state.cwd).name})
            our_run: str | None = next(iter(set(state.runs) - before), None)
            if our_run is not None:
                self._active_runs[request.run_id] = (state.thread_id, our_run)
            deadline = asyncio.get_running_loop().time() + (request.timeout_seconds or self.cfg.t3_max_run_seconds)
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    for run_id in ([our_run] if our_run else list(state.runs)):
                        await self._command(state.thread_id, {
                            "type": "run.interrupt", "runId": run_id, "reason": "Timed out.",
                        })
                    return RunResult(outcome="timed_out", summary="The T3 thread exceeded its time limit.",
                                     session_id=state.thread_id, session_title=state.title)
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=min(5.0, remaining))
                except asyncio.TimeoutError:
                    if our_run is None:
                        our_run = next((r for r in state.runs if r not in before), None)
                    if our_run is not None:
                        self._active_runs[request.run_id] = (state.thread_id, our_run)
                    continue
                await emit(event)
                kind = event.get("event")
                if our_run is None:
                    candidate = event.get("run_id")
                    if isinstance(candidate, str) and candidate not in before:
                        our_run = candidate
                        self._active_runs[request.run_id] = (state.thread_id, our_run)
                if kind in ("turn.completed", "turn.failed", "turn.cancelled") and event.get("run_id") in (
                    our_run, None
                ):
                    self.db.set_thread_binding(
                        repo=repo_key, runner=self.id, thread_id=state.thread_id,
                        project_id=state.project_id, title=state.title,
                        worktree_path=state.worktree_path, branch=state.branch,
                        last_sequence=state.sequence,
                    )
                    if kind == "turn.completed":
                        return RunResult(outcome="pushed", summary=event.get("text") or "Done.",
                                         session_id=state.thread_id, session_title=state.title,
                                         worktree_path=state.worktree_path, branch=state.branch)
                    if kind == "turn.failed":
                        return RunResult(outcome="errored", summary=event.get("error") or "The turn failed.",
                                         session_id=state.thread_id, session_title=state.title,
                                         worktree_path=state.worktree_path, branch=state.branch)
                    return RunResult(outcome="cancelled", summary="Stopped.",
                                     session_id=state.thread_id, session_title=state.title)
                if kind in ("approval.request", "question.request") and state.pending is not None:
                    pending = state.pending
                    answer: str | None = None
                    if request.approval_cb is not None:
                        answer = await request.approval_cb(
                            str(event.get("description") or ""), "", state.thread_id
                        )
                    if answer is None:
                        return RunResult(
                            outcome="deferred_approval",
                            summary=f"The T3 thread is waiting: {event.get('description', '')}",
                            runner=self.id,
                            session_id=state.thread_id, session_title=state.title,
                            worktree_path=state.worktree_path, branch=state.branch,
                            question={"request_id": pending.get("requestId"),
                                      "type": pending.get("type"),
                                      "description": str(event.get("description") or ""),
                                      "questions": question_fields(pending.get("questions", []))},
                        )
                    await self._respond(state, pending, answer)
        finally:
            self._active_runs.pop(request.run_id, None)
            state.listeners.discard(queue)

    async def answer(self, session_id: str, answer: str) -> bool:
        state = self._threads.get(session_id) or (await self._restore_state(session_id))
        if state is None or state.pending is None:
            return False
        return await self._respond(state, state.pending, answer)

    async def _respond(self, state: _ThreadState, item: dict[str, Any], answer: str) -> bool:
        command: dict[str, Any] = {"type": "runtime-request.respond", "requestId": item.get("requestId")}
        if item.get("type") == "approval_request":
            command["decision"] = "accept" if is_approval(answer) else "decline"
        else:
            command["answers"] = form_answer(question_fields(item.get("questions", [])), answer)
        ok = await self._command(state.thread_id, command)
        if ok:
            self._settle(state, item.get("requestId"))
        return ok

    async def interrupt(self, run_id: str) -> bool:
        target = self._active_runs.pop(run_id, None)
        if target is None:
            return False
        thread_id, t3_run = target
        state = self._threads.get(thread_id)
        t3_runs = [t3_run] if t3_run else list(state.runs if state else ())
        stopped = False
        for active_run in t3_runs:
            if not thread_id or not active_run:
                continue
            stopped = await self._command(thread_id, {
                "type": "run.interrupt", "runId": active_run, "reason": "Stopped by the user.",
            }) or stopped
        return stopped

    # ------------------------------------------------------------------ thread resolution

    def _cwd(self, request: RunRequest) -> Path | None:
        if request.cwd is not None:
            return request.cwd
        if request.project:
            return resolve_project(request.project, self.cfg.projects_dir)
        return None

    async def _resolve_thread(
        self, request: RunRequest, cwd: Path, repo_key: str
    ) -> tuple[_ThreadState | None, bool, str | None]:
        snapshot = await self.shell_snapshot()
        projects = snapshot.get("projects", [])
        project = find_project(projects, str(cwd))
        if project is None and cwd.is_relative_to(self.cfg.projects_dir.expanduser().resolve()):
            project = {"id": str(uuid.uuid4()), "workspaceRoot": str(cwd)}
            created = await self._call("orchestration.dispatchCommand", {
                "type": "project.create", "commandId": str(uuid.uuid4()), "projectId": project["id"],
                "title": cwd.name, "workspaceRoot": str(cwd),
            })
            if created is None:
                return None, False, f"T3 could not create a project for {cwd}."

        if request.pinned_session_id:
            thread = self._thread_from_snapshot(snapshot, request.pinned_session_id)
            state = self._state(request.pinned_session_id)
            state.cwd = thread["root"] if thread else str(cwd)
            state.title = thread["title"] if thread else state.title
            state.project_id = thread["projectId"] if thread else (project or {}).get("id")
            await self._subscribe(state)
            return state, False, None

        bound = self.db.get_thread_binding(repo_key)
        if request.session and request.session != "new":
            sessions = await self.list_sessions(str(cwd))
            matches = match_sessions(sessions, request.session)
            if matches:
                chosen = matches[0]
                state = self._state(chosen.id)
                state.cwd, state.title, state.branch = chosen.cwd, chosen.title, chosen.branch
                state.project_id = (project or {}).get("id")
                await self._subscribe(state)
                return state, True, None

        if request.session != "new" and bound is not None and bound.thread_id in {
            t["id"] for t in recent_threads(snapshot, limit=None)
        }:
            thread = self._thread_from_snapshot(snapshot, bound.thread_id)
            state = self._state(bound.thread_id)
            state.cwd = thread["root"] if thread else (bound.worktree_path or str(cwd))
            state.title = thread["title"] if thread else bound.title
            state.branch = thread["branch"] if thread else bound.branch
            state.worktree_path = thread["root"] if thread and thread["worktree"] else bound.worktree_path
            state.project_id = (project or {}).get("id") or bound.project_id
            await self._subscribe(state)
            return state, False, None

        if request.session != "new" and not request.worktree_branch:
            idle = [s for s in await self.list_sessions(str(cwd)) if not s.running]
            if idle:
                chosen = idle[0]
                state = self._state(chosen.id)
                state.cwd, state.title, state.branch = chosen.cwd, chosen.title, chosen.branch
                state.project_id = (project or {}).get("id")
                await self._subscribe(state)
                return state, True, None

        return await self._launch(request, cwd, project, repo_key)

    async def _launch(
        self,
        request: RunRequest,
        cwd: Path,
        project: dict[str, Any] | None,
        repo_key: str,
    ) -> tuple[_ThreadState | None, bool, str | None]:
        defaults = t3_defaults()
        model = (project or {}).get("defaultModelSelection") or defaults.get("defaultModelSelection")
        if not project or not model:
            return None, False, f"T3 has no project holding {cwd}, or no default model in T3 settings."
        thread_id = str(uuid.uuid4())
        strategy: dict[str, Any] = {"type": "root"}
        branch = ""
        if request.worktree_branch:
            branch = request.worktree_branch
            strategy = {"type": "worktree", "baseBranch": "origin/master", "branch": branch}
        title = request.prompt.strip().splitlines()[0][:80] or "Jarvis task"
        # The first message carries the small index plus this repo's note: a new daytime thread starts knowing
        # what the night learned, without pasting the whole memory into every turn after it.
        initial = SYNC_MASTER + request.prompt
        standing = memory.standing_context(self.cfg.home_dir, repo_key)
        if standing:
            initial = f"[Standing context]\n{standing}\n\n{initial}"
        launched = await self._call("orchestration.launchThread", {
            "commandId": str(uuid.uuid4()), "threadId": thread_id, "projectId": project["id"],
            "title": title, "generateTitle": True, "modelSelection": model,
            "runtimeMode": defaults.get("defaultRuntimeMode") or "auto",
            "interactionMode": "default", "workspaceStrategy": strategy,
            "initialMessage": {"text": initial, "attachments": []},
        })
        if launched is None:
            return None, False, "T3 Code refused to start a thread (is a default model set in T3?)."
        state = self._state(launched.get("threadId") or thread_id)
        state.cwd, state.title, state.project_id = str(cwd), title, project["id"]
        state.branch, state.worktree_path = branch, None
        state.initial_message_sent = True
        await self._subscribe(state)
        self.db.set_thread_binding(
            repo=repo_key, runner=self.id, thread_id=state.thread_id, project_id=state.project_id,
            title=state.title, worktree_path=state.worktree_path, branch=state.branch,
        )
        return state, True, None

    async def _restore_state(self, thread_id: str) -> _ThreadState | None:
        try:
            snapshot = await self.shell_snapshot()
        except Exception:
            return None
        thread = self._thread_from_snapshot(snapshot, thread_id)
        if thread is None:
            return None
        state = self._state(thread_id)
        state.cwd, state.title, state.branch = thread["root"], thread["title"], thread["branch"]
        state.project_id = thread["projectId"]
        state.worktree_path = thread["root"] if thread["worktree"] else None
        await self._subscribe(state)
        return state

    @staticmethod
    def _thread_from_snapshot(snapshot: dict[str, Any], thread_id: str) -> dict[str, Any] | None:
        return next((t for t in recent_threads(snapshot, limit=None) if t["id"] == thread_id), None)
