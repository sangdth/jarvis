"""
The session contract every coding client shares (T3 Code, Claude Code, OpenCode, Pi): list a client's sessions,
pick one to continue, start a new one. Each client keeps its sessions in its own store (T3 calls them threads);
Jarvis only reads that store and points the client at a session.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from jarvis.agent_adapter import BaseAgentAdapter, WatchedAgentConfig
from jarvis.state import JarvisState

READY_SECS = 15.0  # how long a session switch waits for the client to come back up (Claude Code starts in ~2 s)


@dataclass(frozen=True)
class AgentSession:
    id: str  # Claude Code session uuid, OpenCode `ses_…`, Pi session id
    title: str  # custom title, summary or first prompt
    cwd: str  # absolute folder the session works in
    updated_at: float  # seconds since the epoch; lists are newest first
    branch: str = ""  # git branch, when the client records it (Claude Code and T3 do)
    running: bool = False  # a turn is running in it right now (T3 reports this)


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


_WORD = re.compile(r"\w+")
# Words that name no session: how people ask for one ("continue the session about ...").
_FILLER = {"the", "and", "for", "that", "this", "with", "about", "session", "sessions", "section", "thread", "one",
           "where", "what", "was", "were", "from", "continue", "open", "switch", "back", "last", "earlier",
           "yesterday", "today", "our", "you", "did", "on", "in", "to", "of", "it", "my", "me", "go", "we", "is",
           "a", "an", "i"}
_STEM = 4  # a word this long matches a longer one it starts ("auth" -> "authentication", "test" -> "tests")


def _hits(wanted: set[str], have: set[str]) -> int:
    return sum(
        1 for w in wanted
        if w in have or len(w) >= _STEM and any(h.startswith(w) or len(h) >= _STEM and w.startswith(h) for h in have)
    )


def match_sessions(sessions: list[AgentSession], words: str) -> list[AgentSession]:
    """
    Sessions that share a word with `words` (title, branch, folder name), most shared words first, newest on a tie.
    Two-letter words count ("pr", "ui", "v2"); a word of 4+ letters also matches one it starts or that starts it.
    shortcut: word overlap only; when titles stop being enough to find a session, rescore with bge-m3 embeddings.
    """
    wanted = {w.lower() for w in _WORD.findall(words) if len(w) > 1} - _FILLER
    scored = []
    for s in sessions:
        have = {w.lower() for w in _WORD.findall(f"{s.title} {s.branch} {Path(s.cwd).name}")}
        if hits := _hits(wanted, have):
            scored.append((hits, s.updated_at, s))
    return [s for _, _, s in sorted(scored, key=lambda t: (t[0], t[1]), reverse=True)]
