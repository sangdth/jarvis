"""The session contract every runner shares: a coding client's sessions, and matching one by spoken words.

A runner's sessions live in the client's own store (T3 Code calls them threads); Jarvis only reads that store
and points the client at a session. `match_sessions` is deliberately lexical: the lists are small and every
session has a title.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

READY_SECS = 15.0  # how long a session switch waits for the client to come back up (Claude Code starts in ~2 s)


@dataclass(frozen=True)
class AgentSession:
    """One conversation a runner can continue."""

    id: str  # Claude Code session uuid, OpenCode `ses_…`, T3 thread id
    title: str  # custom title, summary or first prompt
    cwd: str  # absolute folder the session works in
    updated_at: float  # seconds since the epoch; lists are newest first
    branch: str = ""  # git branch, when the client records it (Claude Code and T3 do)
    running: bool = False  # a turn is running in it right now (T3 reports this)


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
