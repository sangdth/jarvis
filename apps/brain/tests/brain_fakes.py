"""Test doubles for the brain's orchestration seam.

The router is an LLM call; unit tests must never reach OpenRouter. These helpers keep the old
"interactive planner" behavior behind the same interface `BrainServer` calls.
"""

from __future__ import annotations

from typing import Any

from brain.db import BrainDB
from brain.reconciler import Reconciler
from brain.runners.base import RunResult
from jarvis_core.questions import is_approval


class PlannerOrchestrator:
    """Routes every turn to the deterministic `pi` Planner, then runs a ready self-upgrade like the old server."""

    def __init__(self, db: BrainDB, reconciler: Reconciler) -> None:
        self.db = db
        self.reconciler = reconciler

    async def close(self) -> None:
        return None

    async def route(
        self,
        *,
        run_id: str,
        user_input: str,
        session_id: str,
        pinned: dict[str, str] | None,
        approval_cb: Any,
        emit: Any,
        screenshot: str | None = None,
        session_mode: str | None = None,
        cwd: str | None = None,
    ) -> RunResult:
        async def bool_cb(spoken: str, details: str, session: str | None) -> bool | None:
            if approval_cb is None:
                return None
            answer = await approval_cb(spoken, details, session)
            return None if answer is None else is_approval(answer)

        res = await self.reconciler.run_interactive_planner(
            user_input=user_input, run_key=run_id, approval_cb=bool_cb
        )
        if res.outcome == "errored":
            return RunResult(outcome="errored", summary=res.summary)
        notes: list[str] = []
        for tid in res.upserted_task_ids or []:
            task = self.db.get_task(tid)
            if task.repo in ("brain", "jarvis") and task.status == "ready" and task.verify_cmd:
                claimed = self.db.claim_next_task(lease_seconds=1200, task_id=task.id)
                if claimed is not None:
                    done = await self.reconciler.execute_claimed_task(
                        claimed, run_key=run_id, approval_cb=bool_cb
                    )
                    if done.status == "done":
                        notes.append(f"Self-upgrade {done.id} passed verification and was applied.")
                    else:
                        notes.append(f"Self-upgrade {done.id} ended with status {done.status}.")
        return RunResult(
            outcome="pushed",
            summary=" ".join([res.summary, *notes]).strip(),
            session_id=res.session_id,
        )
