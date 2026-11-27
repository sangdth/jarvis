"""One-shot `pi` answers, and the small conversation session behind a follow-up question.

A standalone question runs in a fresh `0700` temp directory and leaves nothing behind. When the caller
supplies a `session_key`, the session lives under `sessions/chat/<key>` instead, so the next turn resumes the
same `pi` transcript (with `pi`'s own compaction) rather than starting cold. Old chat sessions are pruned by
age and count; a board worker never uses this path.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import Any

from brain.config import Settings
from brain.db import BrainDB
from brain.pi_stream import PiStreamState
from brain.runners.base import EventCallback, RunRequest, RunResult
from brain.sandbox import strip_secrets, wrap_ephemeral

TOOLS = "read,grep,find,ls,bash"
# Words that make an answer worth streaming while it is written; the rest of the JSON event is machinery.
_STREAM_PREVIEW = 240
_SAFE_KEY_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


class EphemeralPiRunner:
    """Answers a question with one `pi` process; a chat session is kept only when the caller asks for one."""

    id = "ephemeral"

    def __init__(self, cfg: Settings, db: BrainDB) -> None:
        self.cfg = cfg
        self.db = db
        self._active: dict[str, asyncio.subprocess.Process] = {}

    async def start(self) -> None:  # nothing to keep alive
        return None

    async def stop(self) -> None:
        for proc in list(self._active.values()):
            self._terminate(proc)

    async def answer(self, session_id: str, answer: str) -> bool:
        return False  # a one-shot turn has no question to answer

    async def interrupt(self, run_id: str) -> bool:
        proc = self._active.get(run_id)
        if proc is None or proc.returncode is not None:
            return False
        self._terminate(proc, signal.SIGTERM)
        return True

    async def list_sessions(self, cwd: str | None = None, limit: int = 15) -> list[Any]:
        return []  # a chat session has no picker; the voice just keeps its own key

    @staticmethod
    def _chat_key(raw: str) -> str:
        cleaned = _SAFE_KEY_RE.sub("-", raw.strip())[:64].strip(".-")
        return cleaned or "chat"

    def _chat_session_dir(self, key: str) -> Path:
        path = self.cfg.chat_sessions_dir / key
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
        return path

    def _prune_chat_sessions(self) -> None:
        """Keep the newest N chats and drop anything past the TTL; cheap enough to run after each turn."""
        root = self.cfg.chat_sessions_dir
        if not root.is_dir():
            return
        cutoff = time.time() - max(1, self.cfg.chat_session_ttl_days) * 86400
        sessions = sorted(
            (p for p in root.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for index, folder in enumerate(sessions):
            try:
                too_old = folder.stat().st_mtime < cutoff
                too_big = (
                    sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
                    > max(1, self.cfg.max_chat_session_mb) * 1024 * 1024
                )
            except OSError:
                continue
            if index >= max(1, self.cfg.max_chat_sessions) or too_old or too_big:
                shutil.rmtree(folder, ignore_errors=True)

    async def run(self, request: RunRequest, emit: EventCallback) -> RunResult:
        timeout = request.timeout_seconds or float(self.cfg.ephemeral_timeout_seconds)
        started = time.monotonic()
        session_key = self._chat_key(request.session_key) if request.session_key else None
        persisted = session_key is not None
        if persisted:
            assert session_key is not None
            work_dir = self._chat_session_dir(session_key)
        else:
            work_dir = Path(tempfile.mkdtemp(prefix="brain-ephemeral-"))
            work_dir.chmod(0o700)
        try:
            cmd = [
                self.cfg.pi_bin,
                "-p",
                "--mode",
                "json",
                "--no-approve",
                "--no-skills",
                "--no-prompt-templates",
                "--no-mcp",
                "--no-extensions",
                "--session-dir",
                str(work_dir),
                "-t",
                TOOLS,
            ]
            if persisted:
                cmd.extend(["--session-id", session_key or ""])
            model = (self.cfg.ephemeral_model or self.cfg.planner_model).strip()
            if model:
                cmd.extend(["--model", model])
            for path in request.append_system_paths:
                if path.is_file():
                    cmd.extend(["--append-system-prompt", str(path)])
            cmd.extend(["--", request.prompt])
            cmd = wrap_ephemeral(
                cmd,
                work_dir=work_dir,
                home_dir=self.cfg.home_dir,
                brain_port=self.cfg.brain_port,
                enabled=self.cfg.sandbox_enabled,
            )
            env = self._env(work_dir)

            state = self.db.get_state()
            budget_left = max(0.0, state.daily_budget_usd - state.spent_today_usd)
            if budget_left <= 0:
                return RunResult(outcome="errored", summary="Daily budget reached; no one-shot answer was run.")
            run_cap = (
                float(request.max_cost_usd)
                if request.max_cost_usd is not None
                else float(self.cfg.max_ephemeral_cost_usd)
            )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(request.cwd or self.cfg.home_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            self._active[request.run_id] = proc
            assert proc.stdout is not None
            cost = 0.0
            stream = PiStreamState()
            emitted = ""
            timed_out = False
            over_budget = False
            over_cap = False
            try:
                while True:
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        timed_out = True
                        self._terminate(proc)
                        break
                    try:
                        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=min(2.0, max(0.1, remaining)))
                    except asyncio.TimeoutError:
                        if proc.returncode is not None:
                            break
                        continue
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    stream.feed_line(line)
                    text = stream.last_text
                    if text and len(text) > _STREAM_PREVIEW and text != emitted:
                        emitted = text
                        await emit({"event": "worker.progress", "run_id": request.run_id,
                                    "preview": text[:_STREAM_PREVIEW]})
                    cost = stream.total_cost
                    if cost > budget_left:
                        over_budget = True
                        self._terminate(proc)
                        break
                    if run_cap > 0 and cost > run_cap:
                        over_cap = True
                        self._terminate(proc)
                        break
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._terminate(proc, signal.SIGKILL)
                    await proc.wait()
                stderr = (await proc.stderr.read()).decode("utf-8", errors="replace").strip() if proc.stderr else ""
            finally:
                self._active.pop(request.run_id, None)
                if proc.returncode is None:
                    self._terminate(proc, signal.SIGKILL)
                    await proc.wait()

            if cost > 0:
                self.db.add_spend(cost)
            if timed_out:
                return RunResult(
                    outcome="timed_out",
                    summary=f"One-shot answer exceeded {timeout:.0f}s.",
                    cost_usd=cost,
                )
            if over_budget:
                return RunResult(
                    outcome="errored",
                    summary="One-shot answer stopped: daily budget exceeded.",
                    cost_usd=cost,
                )
            if over_cap:
                return RunResult(
                    outcome="errored",
                    summary=f"One-shot answer stopped: per-run cost cap ${run_cap:.2f} exceeded.",
                    cost_usd=cost,
                )
            if proc.returncode != 0:
                return RunResult(
                    outcome="errored",
                    summary=stderr or stream.last_text or f"pi exited with code {proc.returncode}.",
                    cost_usd=cost,
                )
            return RunResult(outcome="pushed", summary=stream.last_text or "Done.", cost_usd=cost)
        finally:
            if persisted:
                self._prune_chat_sessions()
            else:
                shutil.rmtree(work_dir, ignore_errors=True)

    def _env(self, tmp_dir: Path) -> dict[str, str]:
        env = dict(os.environ)
        # A one-shot answer needs no board socket and must not inherit a stale task identity or git push credentials.
        for key in ("BRAIN_RPC_SOCK", "BRAIN_WORKTREE_DIR", "BRAIN_TASK_ID", "BRAIN_TASK_KIND"):
            env.pop(key, None)
        gh_dir = tmp_dir / "gh"
        gh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        env["HOME"] = env.get("HOME", str(Path.home()))
        env["TMPDIR"] = str(tmp_dir)
        env["GH_CONFIG_DIR"] = str(gh_dir)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "credential.helper"
        env["GIT_CONFIG_VALUE_0"] = ""
        return strip_secrets(env)

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
