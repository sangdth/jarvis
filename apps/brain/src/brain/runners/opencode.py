"""OpenCode, minimal: one `opencode run` turn per task, continuing the same session id.

Deliberately small. The full SSE adapter (live permissions and form questions, scoped to one folder on the
shared service) lived in the voice harness; what survives here is enough for the owner who names OpenCode
or connects to one of its sessions. Enrich it when it earns the work.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from pathlib import Path
from typing import Any

from loguru import logger

from brain.config import Settings
from brain.runners.base import EventCallback, RunRequest, RunResult
from brain.sandbox import strip_gateway_secrets
from jarvis_core.sessions import AgentSession


class OpenCodeRunner:
    id = "opencode"

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self._sessions: dict[str, str] = {}  # cwd -> last session id
        self._active: dict[str, asyncio.subprocess.Process] = {}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        for proc in list(self._active.values()):
            self._terminate(proc)

    async def list_sessions(self, cwd: str | None = None, limit: int = 15) -> list[AgentSession]:
        binary = shutil.which("opencode")
        if binary is None:
            return []
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "api", "GET", "/session", "--format", "json",
                env=strip_gateway_secrets(dict(os.environ)),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd or Path.home()),
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except (OSError, asyncio.TimeoutError) as exc:
            logger.debug(f"[opencode] Could not list sessions: {exc!r}")
            return []
        try:
            payload: Any = json.loads(out.decode("utf-8", errors="replace") or "[]")
        except json.JSONDecodeError:
            return []
        rows = payload.get("data") if isinstance(payload, dict) else payload
        sessions: list[AgentSession] = []
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            location = (row.get("location") or {}).get("directory") or cwd or ""
            sessions.append(AgentSession(
                id=str(row["id"]), title=str(row.get("title") or "(untitled)"), cwd=str(location),
                updated_at=float((row.get("time") or {}).get("updated") or 0) / 1000,
            ))
        return sessions[:limit]

    async def run(self, request: RunRequest, emit: EventCallback) -> RunResult:
        binary = shutil.which("opencode")
        if binary is None:
            return RunResult(outcome="errored", summary="The `opencode` CLI is not on PATH.")
        cwd = request.cwd or (Path.home() / "Projects")
        session_id = request.pinned_session_id or self._sessions.get(str(cwd))
        cmd = [binary, "run", "--format", "json"]
        if session_id:
            cmd.extend(["--session", session_id])
        cmd.extend(["--", request.prompt])
        timeout = request.timeout_seconds or 1800.0
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd), env=strip_gateway_secrets(dict(os.environ)),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._active[request.run_id] = proc
        try:
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                self._terminate(proc, signal.SIGKILL)
                await proc.wait()
                return RunResult(outcome="timed_out", summary="OpenCode exceeded its time limit.")
        finally:
            self._active.pop(request.run_id, None)
            if proc.returncode is None:
                self._terminate(proc, signal.SIGKILL)
                await proc.wait()
        text = out.decode("utf-8", errors="replace").strip()
        detail = err.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            return RunResult(outcome="errored", summary=detail or text or "OpenCode failed.")
        summary = text
        session = session_id
        for line in text.splitlines():
            try:
                event: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            session = str(event.get("sessionID") or event.get("session_id") or session or "") or None
            if event.get("type") in ("text", "assistant_message") and event.get("text"):
                summary = str(event["text"])
        if session:
            self._sessions[str(cwd)] = session
        return RunResult(outcome="pushed", summary=summary or "Done.", session_id=session)

    async def answer(self, session_id: str, answer: str) -> bool:
        return False  # a minimal one-shot turn has no live question

    async def interrupt(self, run_id: str) -> bool:
        proc = self._active.get(run_id)
        if proc is None or proc.returncode is not None:
            return False
        self._terminate(proc, signal.SIGTERM)
        return True

    @staticmethod
    def _terminate(proc: asyncio.subprocess.Process, sig: signal.Signals = signal.SIGTERM) -> None:
        if proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, OSError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
