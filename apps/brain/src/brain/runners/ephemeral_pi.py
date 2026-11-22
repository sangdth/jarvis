"""One-shot `pi` answers: small, self-contained questions that should leave nothing behind.

The worker gets a fresh `0700` temp directory as its whole state (session JSONL included), may read the
repository and the network but write only in that directory, and is killed at the timeout or when the daily
budget runs out. The temp directory is deleted in `finally`, so a question never becomes a session.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import Any

from brain.config import Settings
from brain.db import BrainDB
from brain.runners.base import EventCallback, RunRequest, RunResult
from brain.sandbox import wrap_ephemeral

TOOLS = "read,grep,find,ls,bash"
# Words that make an answer worth streaming while it is written; the rest of the JSON event is machinery.
_STREAM_PREVIEW = 240


def _assistant_text(event: dict[str, Any]) -> str | None:
    message = event.get("message") if isinstance(event.get("message"), dict) else event
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(item.get("text", "")).strip()
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
        ]
        joined = "\n".join(part for part in parts if part)
        return joined or None
    return None


def _event_cost(event: dict[str, Any]) -> float:
    for source in (event, event.get("usage"), event.get("message")):
        if isinstance(source, dict):
            for key in ("cost_usd", "cost"):
                value = source.get(key)
                if isinstance(value, (int, float)):
                    return float(value)
            nested = source.get("cost")
            if isinstance(nested, dict) and isinstance(nested.get("total"), (int, float)):
                return float(nested["total"])
    return 0.0


class EphemeralPiRunner:
    """Answers a question with one `pi` process and destroys its session when the answer is in."""

    id = "ephemeral"

    def __init__(self, cfg: Settings, db: BrainDB) -> None:
        self.cfg = cfg
        self.db = db

    async def start(self) -> None:  # nothing to keep alive
        return None

    async def stop(self) -> None:
        return None

    async def answer(self, session_id: str, answer: str) -> bool:
        return False  # a one-shot turn has no question to answer

    async def interrupt(self, run_id: str) -> bool:
        return False

    async def list_sessions(self, cwd: str | None = None, limit: int = 15) -> list[Any]:
        return []  # nothing survives, so there is nothing to list

    async def run(self, request: RunRequest, emit: EventCallback) -> RunResult:
        timeout = request.timeout_seconds or float(self.cfg.ephemeral_timeout_seconds)
        started = time.monotonic()
        tmp_dir = Path(tempfile.mkdtemp(prefix="brain-ephemeral-"))
        tmp_dir.chmod(0o700)
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
                str(tmp_dir),
                "-t",
                TOOLS,
            ]
            model = (self.cfg.ephemeral_model or self.cfg.planner_model).strip()
            if model:
                cmd.extend(["--model", model])
            cmd.extend(["--", request.prompt])
            cmd = wrap_ephemeral(
                cmd,
                work_dir=tmp_dir,
                home_dir=self.cfg.home_dir,
                enabled=self.cfg.sandbox_enabled,
            )
            env = self._env(tmp_dir)

            state = self.db.get_state()
            budget_left = max(0.0, state.daily_budget_usd - state.spent_today_usd)
            if budget_left <= 0:
                return RunResult(outcome="errored", summary="Daily budget reached; no one-shot answer was run.")

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(request.cwd or self.cfg.home_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            assert proc.stdout is not None
            cost = 0.0
            last_text = ""
            timed_out = False
            over_budget = False
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
                    try:
                        event: dict[str, Any] = json.loads(line)
                    except json.JSONDecodeError:
                        last_text = line
                        continue
                    text = _assistant_text(event)
                    if text:
                        last_text = text
                        if len(text) > _STREAM_PREVIEW:
                            await emit({"event": "worker.progress", "run_id": request.run_id,
                                        "preview": text[:_STREAM_PREVIEW]})
                    cost += _event_cost(event)
                    if cost > budget_left:
                        over_budget = True
                        self._terminate(proc)
                        break
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._terminate(proc, signal.SIGKILL)
                    await proc.wait()
                stderr = (await proc.stderr.read()).decode("utf-8", errors="replace").strip() if proc.stderr else ""
            finally:
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
            if proc.returncode != 0:
                return RunResult(
                    outcome="errored",
                    summary=stderr or last_text or f"pi exited with code {proc.returncode}.",
                    cost_usd=cost,
                )
            return RunResult(outcome="pushed", summary=last_text or "Done.", cost_usd=cost)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _env(self, tmp_dir: Path) -> dict[str, str]:
        env = dict(os.environ)
        # A one-shot answer needs no board socket and must not inherit a stale task identity.
        for key in ("BRAIN_RPC_SOCK", "BRAIN_WORKTREE_DIR", "BRAIN_TASK_ID", "BRAIN_TASK_KIND"):
            env.pop(key, None)
        env["HOME"] = env.get("HOME", str(Path.home()))
        env["TMPDIR"] = str(tmp_dir)
        return env

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
