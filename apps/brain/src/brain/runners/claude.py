"""Claude Code, minimal: one `claude -p` turn per task, the user's own settings, the same session resumed.

This is deliberately small. Claude Code is not the default coding client (T3 is), so it runs only when the
owner names it or connects to it, and it keeps only what is needed to continue a session. Live approvals,
AskUserQuestion and the richer streaming come back when the client is enriched.
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

_LITE_BUF = 65536


def _claude_projects_dir() -> Path:
    custom = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = Path(custom).expanduser() if custom else (Path.home() / ".claude")
    return base / "projects"


def _parse_claude_session(path: Path, target_cwd: Path | None, default_cwd: str) -> AgentSession | None:
    try:
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            head_bytes = handle.read(_LITE_BUF)
            if not head_bytes:
                return None
            head = head_bytes.decode("utf-8", errors="replace")
            tail_offset = max(0, stat.st_size - _LITE_BUF)
            if tail_offset == 0:
                tail = head
            else:
                handle.seek(tail_offset)
                tail = handle.read(_LITE_BUF).decode("utf-8", errors="replace")
    except OSError:
        return None
    first_line = head.split("\n", 1)[0]
    if '"isSidechain":true' in first_line or '"isSidechain": true' in first_line:
        return None
    session_cwd = ""
    branch = ""
    title = ""
    first_prompt = ""
    for chunk in (head, tail):
        for raw_line in chunk.splitlines():
            if not raw_line.startswith("{"):
                continue
            try:
                entry = json.loads(raw_line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("isSidechain"):
                return None
            session_cwd = str(entry.get("cwd") or session_cwd)
            branch = str(entry.get("gitBranch") or branch)
            title = str(
                entry.get("customTitle") or entry.get("aiTitle") or entry.get("summary") or title
            ).strip()
            if not first_prompt and entry.get("type") == "user" and not entry.get("isMeta"):
                msg = entry.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(content, str):
                    first_prompt = " ".join(content.split())[:200]
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                            first_prompt = " ".join(str(block["text"]).split())[:200]
                            break
    resolved_cwd = session_cwd or default_cwd
    if target_cwd is not None:
        if not session_cwd:
            return None
        try:
            cand = Path(session_cwd).expanduser().resolve()
        except OSError:
            return None
        if cand != target_cwd and not cand.is_relative_to(target_cwd):
            return None
    label = title or first_prompt
    if not label:
        return None
    return AgentSession(
        id=path.stem,
        title=label,
        cwd=resolved_cwd,
        updated_at=stat.st_mtime,
        branch=branch,
    )


def _list_claude_sessions(cwd: str | None, limit: int, default_cwd: str) -> list[AgentSession]:
    projects = _claude_projects_dir()
    if not projects.is_dir():
        return []
    target_cwd = Path(cwd).expanduser().resolve() if cwd else None
    files: list[tuple[float, Path]] = []
    for jsonl in projects.glob("*/*.jsonl"):
        try:
            files.append((jsonl.stat().st_mtime, jsonl))
        except OSError:
            continue
    files.sort(key=lambda item: item[0], reverse=True)
    sessions: list[AgentSession] = []
    for _, path in files:
        parsed = _parse_claude_session(path, target_cwd, default_cwd)
        if parsed is not None:
            sessions.append(parsed)
            if len(sessions) >= limit:
                break
    return sessions


class ClaudeRunner:
    id = "claude"

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self._sessions: dict[str, str] = {}  # cwd -> last session id (Claude names it)
        self._active: dict[str, asyncio.subprocess.Process] = {}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        for proc in list(self._active.values()):
            self._terminate(proc)

    async def list_sessions(self, cwd: str | None = None, limit: int = 15) -> list[AgentSession]:
        try:
            return await asyncio.to_thread(
                _list_claude_sessions, cwd, limit, str(cwd or self.cfg.home_dir)
            )
        except Exception as exc:
            logger.debug(f"[claude] Could not list sessions: {exc!r}")
            return []

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
                return RunResult(outcome="timed_out", summary="Claude Code exceeded its time limit.")
        finally:
            self._active.pop(request.run_id, None)
            if proc.returncode is None:
                self._terminate(proc, signal.SIGKILL)
                await proc.wait()
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
