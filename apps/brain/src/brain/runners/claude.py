"""Claude Code, minimal: one `claude -p` turn per task, the user's own settings, the same session resumed.

This is deliberately small. Claude Code is not the default coding client (T3 is), so it runs only when the
owner names it or connects to it, and it keeps only what is needed to continue a session. Live approvals,
AskUserQuestion and the richer streaming come back when the client is enriched.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from loguru import logger

from brain.config import Settings
from brain.runners.base import EventCallback, RunRequest, RunResult
from jarvis_core.sessions import AgentSession


class ClaudeRunner:
    id = "claude"

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self._sessions: dict[str, str] = {}  # cwd -> last session id (Claude names it)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def list_sessions(self, cwd: str | None = None, limit: int = 15) -> list[AgentSession]:
        try:
            from claude_agent_sdk import list_sessions
        except ImportError:
            return []
        try:
            found = await asyncio.to_thread(list_sessions, directory=cwd, limit=limit)
        except Exception as exc:
            logger.debug(f"[claude] Could not list sessions: {exc!r}")
            return []
        return [
            AgentSession(
                id=s.session_id,
                title=s.custom_title or s.summary or s.first_prompt or "(untitled)",
                cwd=s.cwd or cwd or str(self.cfg.home_dir),
                updated_at=s.last_modified / 1000,
                branch=s.git_branch or "",
            )
            for s in found
        ]

    async def run(self, request: RunRequest, emit: EventCallback) -> RunResult:
        binary = shutil.which("claude")
        if binary is None:
            return RunResult(outcome="errored", summary="The `claude` CLI is not on PATH.")
        cwd = request.cwd or (Path.home() / "Projects")
        session_id = request.pinned_session_id or self._sessions.get(str(cwd))
        cmd = [binary, "-p", request.prompt, "--output-format", "json"]
        if session_id:
            cmd.extend(["--resume", session_id])
        timeout = request.timeout_seconds or 1800.0
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return RunResult(outcome="timed_out", summary="Claude Code exceeded its time limit.")
        text = out.decode("utf-8", errors="replace").strip()
        detail = err.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            return RunResult(outcome="errored", summary=detail or text or "Claude Code failed.")
        try:
            payload: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError:
            return RunResult(outcome="pushed", summary=text or "Done.")
        if payload.get("session_id"):
            self._sessions[str(cwd)] = str(payload["session_id"])
        if payload.get("is_error"):
            return RunResult(outcome="errored", summary=str(payload.get("result") or "The turn failed."))
        return RunResult(
            outcome="pushed",
            summary=str(payload.get("result") or text or "Done."),
            session_id=str(payload.get("session_id") or "") or None,
        )

    async def answer(self, session_id: str, answer: str) -> bool:
        return False  # a minimal one-shot turn has no live question

    async def interrupt(self, run_id: str) -> bool:
        return False
