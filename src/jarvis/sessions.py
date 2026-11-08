"""
The session contract the coding clients other than T3 Code share (Claude Code, OpenCode, Pi): list a client's
sessions, pick one to continue, start a new one. Each client keeps its sessions in its own store; Jarvis only
reads that store and points the client at a session. T3 Code has its own threads and projects (`t3_adapter.py`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from jarvis.agent_adapter import BaseAgentAdapter


@dataclass(frozen=True)
class AgentSession:
    id: str  # Claude Code session uuid, OpenCode `ses_…`, Pi session id
    title: str  # custom title, summary or first prompt
    cwd: str  # absolute folder the session works in
    updated_at: float  # seconds since the epoch; lists are newest first
    branch: str = ""  # git branch, when the client records it (Claude Code does)


class CodingSessionAdapter(BaseAgentAdapter):
    """A coding client whose sessions Jarvis can list, continue and start."""

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
            await self.start()


_WORD = re.compile(r"\w+")
# Words that name no session: how people ask for one ("continue the session about ...").
_FILLER = {"the", "and", "for", "that", "this", "with", "about", "session", "one", "where", "what", "was", "were",
           "from", "continue", "last", "earlier", "yesterday", "today", "our", "you", "did"}


def match_sessions(sessions: list[AgentSession], words: str) -> list[AgentSession]:
    """
    Sessions that share a word with `words` (title, branch, folder name), most shared words first, newest on a tie.
    shortcut: word overlap only; when titles stop being enough to find a session, rescore with bge-m3 embeddings.
    """
    wanted = {w.lower() for w in _WORD.findall(words) if len(w) > 2} - _FILLER  # "on", "my": too short to name one
    scored = []
    for s in sessions:
        have = {w.lower() for w in _WORD.findall(f"{s.title} {s.branch} {Path(s.cwd).name}")}
        if hits := len(wanted & have):
            scored.append((hits, s.updated_at, s))
    return [s for _, _, s in sorted(scored, key=lambda t: (t[0], t[1]), reverse=True)]
