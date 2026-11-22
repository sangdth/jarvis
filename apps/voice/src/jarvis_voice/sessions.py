"""
The session contract every coding client shares (T3 Code, Claude Code, OpenCode, Pi): list a client's sessions,
pick one to continue, start a new one. Each client keeps its sessions in its own store (T3 calls them threads);
Jarvis only reads that store and points the client at a session.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger

from jarvis_core.sessions import READY_SECS, AgentSession, match_sessions
from jarvis_voice.agent_adapter import BaseAgentAdapter, WatchedAgentConfig
from jarvis_voice.state import JarvisState

__all__ = ["AgentSession", "CodingSessionAdapter", "READY_SECS", "match_sessions"]


class CodingSessionAdapter(BaseAgentAdapter):
    """A coding client whose sessions Jarvis can list, continue and start."""

    def __init__(self, config: WatchedAgentConfig, state: JarvisState) -> None:
        super().__init__(config, state)
        self.cwd = str(Path(config.cwd).expanduser().resolve())  # the folder the current session works in
        self._ready = asyncio.Event()  # set by the watch loop once the client can take a task

    @property
    def session_id(self) -> str | None:
        """The session tasks go to now; None before the first one."""
        return None

    async def list_sessions(self, cwd: str | None = None, limit: int = 15) -> list[AgentSession]:
        """The client's sessions in cwd (every folder when None), newest first. Raises when the client is unreachable."""
        raise NotImplementedError

    async def pick_session(self, session: AgentSession, connected: bool = False) -> bool:
        """Send every task to this session from now on. False when the switch can't happen now (a turn is running)."""
        raise NotImplementedError

    async def new_session(self, cwd: str | None = None) -> bool:
        """Start a clean conversation in cwd (the current folder when None). False when the client can't."""
        raise NotImplementedError

    async def _restart(self) -> None:
        """Run the watch loop again for a new session. Before start_all (`jarvis connect`) there is none to restart."""
        if self._task is not None:
            await BaseAgentAdapter.stop(self)  # cancels the loop only: no subclass cleanup like closing the client
            self._ready.clear()
            await self.start()
            # A task said with the switch ("new session and run the tests") is sent right after this returns.
            try:
                await asyncio.wait_for(self._ready.wait(), READY_SECS)
            except TimeoutError:
                logger.warning(f"[{self.config.id}] Not connected {READY_SECS:.0f} s after switching sessions.")


