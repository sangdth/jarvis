"""
Pi (pi.dev coding agent) through an open Pi window: `defaults/pi/jarvis.ts`, a Pi extension, opens a Unix socket per
window in SOCKET_DIR. Jarvis sends prompts and stops there and hears when a turn settles. Pi asks no approvals of its
own; an extension's dialog shows in the window and is answered there, never by Jarvis.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from jarvis.agent_adapter import WatchedAgentConfig
from jarvis.config import SHARED_HOME
from jarvis.sessions import AgentSession, CodingSessionAdapter
from jarvis.state import JarvisState

SOCKET_DIR = SHARED_HOME / "pi"  # one folder for a dev and an installed Jarvis, as the extension hardcodes
EXTENSION = Path(__file__).parent / "defaults" / "pi" / "jarvis.ts"
PI_EXTENSIONS = Path("~/.pi/agent/extensions").expanduser()


def install_extension(target_dir: Path = PI_EXTENSIONS) -> bool:
    """Put Jarvis's extension in Pi's global extensions folder. True when it was missing or out of date."""
    target = target_dir / EXTENSION.name
    source = EXTENSION.read_text(encoding="utf-8")
    if target.exists() and target.read_text(encoding="utf-8") == source:
        return False
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return True


async def _hello(path: Path) -> dict[str, Any] | None:
    """The window's hello, or None when nothing listens there."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), 1.0)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        record = json.loads(await asyncio.wait_for(reader.readline(), 1.0))
        return record if record.get("type") == "hello" else None
    except (OSError, ValueError, asyncio.TimeoutError):
        return None
    finally:
        writer.close()


async def list_windows(socket_dir: Path = SOCKET_DIR) -> list[dict[str, Any]]:
    """Open Pi windows with the extension, newest first. A socket nobody answers on (a crashed window) is deleted."""
    windows = []
    for path in sorted(socket_dir.glob("*.sock"), key=lambda p: p.stat().st_mtime, reverse=True):
        hello = await _hello(path)
        if hello is None:
            path.unlink(missing_ok=True)
            continue
        windows.append({**hello, "socket": str(path), "mtime": path.stat().st_mtime})
    return windows


class PiAdapter(CodingSessionAdapter):
    """
    One Pi window, reached through its socket. Created by `jarvis connect pi`, not from agents.json. Its sessions are
    the windows open now: a closed one can't be continued from here, and a new one is opened by the user.
    """

    def __init__(self, config: WatchedAgentConfig, state: JarvisState, socket_path: str = "") -> None:
        super().__init__(config, state)
        self.socket_path = socket_path
        self._writer: asyncio.StreamWriter | None = None
        self._running = False
        self._dialog = False  # a dialog is open in the window

    async def _run_loop(self) -> None:
        misses = 0
        while True:
            try:
                reader, self._writer = await asyncio.open_unix_connection(self.socket_path)
                misses = 0
                logger.info(f"Connected to the Pi window at {self.socket_path}")
                while line := await reader.readline():
                    try:
                        await self._handle(json.loads(line))
                    except ValueError:
                        continue
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                misses += 1
                logger.debug(f"[{self.config.id}] Pi socket unavailable ({exc!r}).")
                if misses == 3:  # /reload reopens the socket within a second; three misses is a closed window
                    self._running = False
                    await self._surface("closed its window. Run jarvis connect pi again to pick another.")
            self._writer = None
            await asyncio.sleep(1.0)

    async def _handle(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        if kind == "hello":
            self._running = bool(record.get("busy"))
            logger.info(f"[{self.config.id}] Pi session {record.get('session')} in {record.get('cwd')}")
        elif kind == "busy":
            self._running = True
        elif kind == "settled":
            self._running = False
            if self._dialog:
                self._dialog = False
                self._question_settled()
            if not record.get("aborted") and record.get("text"):
                await self._surface(record["text"], is_report=True)
        elif kind == "prompt_start":
            self._dialog = True
            await self._surface(f"is waiting for you in its window: {record.get('title') or 'a question'}")
        elif kind == "prompt_end" and self._dialog:
            self._dialog = False
            self._question_settled()

    async def _write(self, record: dict[str, Any]) -> bool:
        if self._writer is None:
            logger.warning(f"[{self.config.id}] Not connected to the Pi window.")
            return False
        try:
            self._writer.write(json.dumps(record).encode() + b"\n")
            await self._writer.drain()
            return True
        except OSError as exc:
            logger.error(f"Failed to reach the Pi window: {exc!r}")
            return False

    async def send_task(self, task: str) -> bool:
        ok = await self._write({"type": "prompt", "text": task})
        self._running = self._running or ok  # Pi's busy comes next; a stop said before it still finds the turn
        return ok

    async def answer(self, answer: str) -> bool | str:
        # Pi's dialogs belong to its extensions and can't be answered from outside the window.
        return "answer_in_pi_window" if self._dialog else False

    async def interrupt(self) -> bool:
        ok = await self._write({"type": "abort"})
        if ok:
            self._running = False
        return ok

    @property
    def busy(self) -> bool:
        return self._running

    async def list_sessions(self, cwd: str | None = None, limit: int = 15) -> list[AgentSession]:
        return [
            AgentSession(id=w["session"], title=w["name"] or w["first"] or "(new session)", cwd=w["cwd"], updated_at=w["mtime"])
            for w in await list_windows()
            if cwd is None or w["cwd"] == cwd
        ][:limit]

    async def pick_session(self, session: AgentSession, connected: bool = False) -> bool:
        if self.busy:
            return False
        window = next((w for w in await list_windows() if w["session"] == session.id), None)
        if window is None:  # closed since it was listed
            return False
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        self.socket_path, self.cwd, self.connected = window["socket"], window["cwd"], connected
        await self._restart()
        return True

    async def new_session(self, cwd: str | None = None) -> bool:
        logger.info(f"[{self.config.id}] A new Pi session is a new Pi window; Jarvis can't open one.")
        return False

    async def stop(self) -> None:
        await super().stop()
        if self._writer is not None:
            self._writer.close()
