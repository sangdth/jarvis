"""What every execution runner shares: a request, its result, and the small protocol the brain drives it through.

`brain` owns the board and the conversation state; a runner owns one client's way of doing the work. T3 Code
threads, one-shot `pi` answers, Claude Code and OpenCode all implement this, so the orchestrator never needs a
special case per client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Protocol

from jarvis_core.sessions import AgentSession

RunOutcome = Literal[
    "pushed",  # the work finished (T3/Claude/OpenCode) or the answer is ready (ephemeral pi)
    "failed_verify",
    "deferred_approval",
    "timed_out",
    "errored",
    "cancelled",
]

# A runner reports a question, an approval, a switch or a finished turn by calling this. The brain publishes it
# on the run's SSE stream; the voice speaks questions and approvals verbatim (never through an LLM).
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
# The answer to a question or approval, in the user's words (None: nobody answered in time).
ApprovalCallback = Callable[[str, str, str | None], Awaitable[str | None]]


@dataclass
class RunRequest:
    """One task, as the orchestrator hands it to a runner."""

    run_id: str
    prompt: str
    # A listed project folder (relative to projects_dir) or an explicit cwd.
    project: str | None = None
    cwd: Path | None = None
    # None to continue the bound session, "new" for a fresh one, or words naming an earlier session.
    session: str | None = None
    # Set by `jarvis connect`: every task goes to this client session for the whole run.
    pinned_session_id: str | None = None
    # A board task: run in a new worktree on this branch and leave the work there for verification.
    worktree_branch: str | None = None
    # Board work waits for the owner when a client asks; a voice turn asks live.
    approval_cb: ApprovalCallback | None = None
    screenshot: str | None = None
    timeout_seconds: float | None = None


@dataclass
class RunResult:
    outcome: RunOutcome
    summary: str
    cost_usd: float = 0.0
    session_id: str | None = None  # the thread/session the work went to
    session_title: str | None = None
    worktree_path: str | None = None
    branch: str = ""
    diagnostic: str | None = None
    question: dict[str, Any] | None = None  # set when outcome is deferred_approval
    events: list[dict[str, Any]] = field(default_factory=list)


class Runner(Protocol):
    """One way to do work: a coding client's session, or a one-shot answer."""

    id: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def run(self, request: RunRequest, emit: EventCallback) -> RunResult: ...
    async def answer(self, session_id: str, answer: str) -> bool: ...
    async def interrupt(self, run_id: str) -> bool: ...
    async def list_sessions(self, cwd: str | None = None, limit: int = 15) -> list[AgentSession]: ...
