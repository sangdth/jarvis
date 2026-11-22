from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

_METRIC_COMPARATOR_RE = re.compile(r"(?<![-=!<>])(<=|>=|==|<|>)\s*(-?\d+(?:\.\d+)?)")


def has_metric_comparator(metric_target: str | None) -> bool:
    return bool(metric_target and _METRIC_COMPARATOR_RE.search(metric_target))


def check_metric_target(metric_target: str | None, metric_value: float | None) -> bool:
    """Evaluates whether `metric_value` satisfies a quantitative comparator in `metric_target`."""
    if not metric_target or metric_value is None:
        return True
    match = _METRIC_COMPARATOR_RE.search(metric_target)
    if not match:
        return True
    op, raw_val = match.group(1), float(match.group(2))
    if op == "<":
        return metric_value < raw_val
    if op == "<=":
        return metric_value <= raw_val
    if op == ">":
        return metric_value > raw_val
    if op == ">=":
        return metric_value >= raw_val
    return abs(metric_value - raw_val) < 1e-9


def is_better_metric(
    metric_target: str | None, candidate: float, incumbent: float | None
) -> bool:
    """Returns True when `candidate` strictly improves over `incumbent` given `metric_target` direction."""
    if incumbent is None:
        return True
    target_text = (metric_target or "").lower()
    match = _METRIC_COMPARATOR_RE.search(target_text)
    if match:
        minimize = match.group(1) in ("<", "<=")
    else:
        minimize = any(
            k in target_text for k in ("min", "latency", "ms", "cycle", "cost", "error", "loss")
        )
    return candidate < incumbent if minimize else candidate > incumbent

Mode = Literal["ASSIST", "AUTONOMOUS"]
TaskKind = Literal["goal", "problem"]
TaskStatus = Literal["draft", "ready", "in_progress", "done", "blocked"]
ChildStrategy = Literal["all", "any"]
WorkerRole = Literal["planner", "builder", "improver"]
SkillStatus = Literal["active", "retired"]
AttemptOutcome = Literal[
    "pushed",
    "decomposed",
    "failed_verify",
    "deferred_approval",
    "timed_out",
    "errored",
    "reaped",
]
OutboxKind = Literal["approval_request", "morning_brief", "blocker", "report"]
OutboxStatus = Literal["queued", "spoken", "approved", "rejected"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_date_str() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


@dataclass(frozen=True)
class SystemState:
    mode: Mode
    daily_budget_usd: float
    spent_today_usd: float
    spend_date: str
    last_good_sha: str | None = None
    upgrade_runs: int = 0
    upgrade_failures: int = 0

    @property
    def budget_remaining_usd(self) -> float:
        return max(0.0, self.daily_budget_usd - self.spent_today_usd)


@dataclass(frozen=True)
class TaskRow:
    id: str
    parent_id: str | None
    kind: TaskKind
    repo: str
    title: str
    spec: str
    metric_target: str | None
    verify_cmd: str | None
    status: TaskStatus
    priority: int
    strike_count: int
    lease_until: str | None
    created_at: str
    updated_at: str
    child_strategy: ChildStrategy = "all"
    best_metric: float | None = None
    best_branch: str | None = None
    best_sha: str | None = None


@dataclass(frozen=True)
class AttemptRow:
    id: int
    task_id: str
    role: WorkerRole
    pi_session_id: str | None
    session_jsonl_path: str | None
    summary: str
    git_branch: str | None
    git_sha: str | None
    cost_usd: float
    outcome: AttemptOutcome
    verify_output: str | None
    created_at: str
    metric_value: float | None = None
    skills_used: tuple[str, ...] = field(default_factory=tuple)
    diagnostic: str | None = None
    reflected: bool = False


@dataclass(frozen=True)
class SkillRow:
    slug: str
    name: str
    description: str
    version: int
    sha256: str
    status: SkillStatus
    content: str
    previous_content: str | None
    lesson: str
    evidence_attempt_ids: tuple[int, ...]
    loads_since_update: int
    successes_since_update: int
    failures_since_update: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class OutboxRow:
    id: str
    kind: OutboxKind
    task_id: str | None
    resume_session_id: str | None
    summary_spoken: str
    details_json: str
    status: OutboxStatus
    created_at: str


class BrainDB:
    """Thread-safe SQLite WAL state store for Superbrain."""

    def __init__(
        self,
        db_path: Path,
        allowed_repos: set[str],
        default_mode: Mode = "ASSIST",
        daily_budget_usd: float = 200.0,
    ) -> None:
        self.db_path = db_path
        self.allowed_repos = {r.strip().strip("/") for r in allowed_repos if r.strip().strip("/")}
        self._lock = threading.RLock()

        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.db_path.parent, 0o700)

        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if self.db_path.exists():
            os.chmod(self.db_path, 0o600)

        schema_sql = files("superbrain").joinpath("schema.sql").read_text(encoding="utf-8")
        with self._lock, self._conn:
            self._conn.executescript(schema_sql)
            self._ensure_attempt_columns_locked()
            self._conn.execute(
                """
                INSERT INTO system_state (id, mode, daily_budget_usd, spent_today_usd, spend_date)
                VALUES (1, ?, ?, 0.0, ?)
                ON CONFLICT(id) DO UPDATE SET daily_budget_usd = excluded.daily_budget_usd
                """,
                (default_mode, daily_budget_usd, _local_date_str()),
            )

    def _ensure_attempt_columns_locked(self) -> None:
        sys_cols = {
            str(r["name"])
            for r in self._conn.execute("PRAGMA table_info(system_state)").fetchall()
        }
        if "last_good_sha" not in sys_cols:
            self._conn.execute("ALTER TABLE system_state ADD COLUMN last_good_sha TEXT")
        if "upgrade_runs" not in sys_cols:
            self._conn.execute(
                "ALTER TABLE system_state ADD COLUMN upgrade_runs INTEGER NOT NULL DEFAULT 0"
            )
        if "upgrade_failures" not in sys_cols:
            self._conn.execute(
                "ALTER TABLE system_state ADD COLUMN upgrade_failures INTEGER NOT NULL DEFAULT 0"
            )

        task_cols = {
            str(r["name"])
            for r in self._conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "child_strategy" not in task_cols:
            self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN child_strategy TEXT NOT NULL DEFAULT 'all'"
            )
        if "best_metric" not in task_cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN best_metric REAL")
        if "best_branch" not in task_cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN best_branch TEXT")
        if "best_sha" not in task_cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN best_sha TEXT")

        cols = {
            str(r["name"])
            for r in self._conn.execute("PRAGMA table_info(attempts)").fetchall()
        }
        if "metric_value" not in cols:
            self._conn.execute("ALTER TABLE attempts ADD COLUMN metric_value REAL")
        if "skills_used" not in cols:
            self._conn.execute("ALTER TABLE attempts ADD COLUMN skills_used TEXT NOT NULL DEFAULT '[]'")
        if "diagnostic" not in cols:
            self._conn.execute("ALTER TABLE attempts ADD COLUMN diagnostic TEXT")
        if "reflected" not in cols:
            self._conn.execute("ALTER TABLE attempts ADD COLUMN reflected INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def validate_repo(self, repo: str) -> str:
        cleaned = repo.strip().strip("/")
        if not cleaned or ".." in Path(cleaned).parts:
            raise ValueError(f"Invalid repository path: {repo!r}")
        if cleaned not in self.allowed_repos:
            raise ValueError(
                f"Repository {cleaned!r} is not in ALLOWED_REPOS ({sorted(self.allowed_repos)})."
            )
        return cleaned

    def get_state(self) -> SystemState:
        today = _local_date_str()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM system_state WHERE id = 1").fetchone()
            assert row is not None
            if row["spend_date"] != today:
                self._conn.execute(
                    """
                    UPDATE system_state
                    SET spent_today_usd = 0.0, spend_date = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    (today, _iso(_utc_now())),
                )
                row = self._conn.execute("SELECT * FROM system_state WHERE id = 1").fetchone()
                assert row is not None
            return SystemState(
                mode=row["mode"],
                daily_budget_usd=float(row["daily_budget_usd"]),
                spent_today_usd=float(row["spent_today_usd"]),
                spend_date=row["spend_date"],
                last_good_sha=row["last_good_sha"] if "last_good_sha" in row.keys() else None,
                upgrade_runs=int(row["upgrade_runs"]) if "upgrade_runs" in row.keys() else 0,
                upgrade_failures=(
                    int(row["upgrade_failures"]) if "upgrade_failures" in row.keys() else 0
                ),
            )

    def set_last_good_sha(self, sha: str) -> SystemState:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE system_state
                SET last_good_sha = ?, upgrade_runs = 0, upgrade_failures = 0, updated_at = ?
                WHERE id = 1
                """,
                (sha.strip(), _iso(_utc_now())),
            )
        return self.get_state()

    def clear_last_good_sha(self) -> SystemState:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE system_state
                SET last_good_sha = NULL, upgrade_runs = 0, upgrade_failures = 0, updated_at = ?
                WHERE id = 1
                """,
                (_iso(_utc_now()),),
            )
        return self.get_state()

    def record_post_upgrade_outcome(self, *, succeeded: bool) -> str | None:
        """
        Tracks runtime health of the active Superbrain self-upgrade. If the first 2 task attempts
        after a self-upgrade fail with 0 successes, clears probation and returns `last_good_sha`
        for automatic git rollback + daemon restart. Once a post-upgrade attempt succeeds (or the
        2-attempt window passes without 2/2 failures), clears `last_good_sha` so unrelated future
        restarts never roll back to an old commit.
        """
        with self._lock, self._conn:
            st = self.get_state()
            if not st.last_good_sha:
                return None
            runs = st.upgrade_runs + 1
            fails = st.upgrade_failures + (0 if succeeded else 1)
            now = _iso(_utc_now())
            if fails >= 2 and runs == fails:
                rollback_sha = st.last_good_sha
                self._conn.execute(
                    """
                    UPDATE system_state
                    SET last_good_sha = NULL, upgrade_runs = 0, upgrade_failures = 0, updated_at = ?
                    WHERE id = 1
                    """,
                    (now,),
                )
                return rollback_sha
            if succeeded or runs >= 2:
                self._conn.execute(
                    """
                    UPDATE system_state
                    SET last_good_sha = NULL, upgrade_runs = 0, upgrade_failures = 0, updated_at = ?
                    WHERE id = 1
                    """,
                    (now,),
                )
                return None
            self._conn.execute(
                """
                UPDATE system_state
                SET upgrade_runs = ?, upgrade_failures = ?, updated_at = ?
                WHERE id = 1
                """,
                (runs, fails, now),
            )
            return None

    def set_mode(self, mode: Mode) -> SystemState:
        if mode not in ("ASSIST", "AUTONOMOUS"):
            raise ValueError(f"Invalid mode: {mode!r}")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE system_state SET mode = ?, updated_at = ? WHERE id = 1",
                (mode, _iso(_utc_now())),
            )
        return self.get_state()

    def add_spend(self, cost_usd: float) -> SystemState:
        if cost_usd <= 0:
            return self.get_state()
        with self._lock, self._conn:
            self.get_state()
            self._conn.execute(
                """
                UPDATE system_state
                SET spent_today_usd = spent_today_usd + ?, updated_at = ?
                WHERE id = 1
                """,
                (float(cost_usd), _iso(_utc_now())),
            )
        return self.get_state()

    def _next_task_id(self, kind: TaskKind) -> str:
        prefix = "SG" if kind == "goal" else "PRB"
        rows = self._conn.execute(
            "SELECT id FROM tasks WHERE id LIKE ?", (f"{prefix}-%",)
        ).fetchall()
        max_num = 0
        for row in rows:
            suffix = str(row["id"]).removeprefix(f"{prefix}-").split(".")[0]
            if suffix.isdigit():
                max_num = max(max_num, int(suffix))
        return f"{prefix}-{max_num + 1}"

    def upsert_task(
        self,
        *,
        kind: TaskKind,
        repo: str,
        title: str,
        spec: str,
        task_id: str | None = None,
        parent_id: str | None = None,
        metric_target: str | None = None,
        verify_cmd: str | None = None,
        priority: int = 50,
        child_strategy: ChildStrategy | None = None,
        ready: bool | None = None,
    ) -> TaskRow:
        cleaned_repo = self.validate_repo(repo)
        if kind not in ("goal", "problem"):
            raise ValueError(f"Invalid task kind: {kind!r}")
        if child_strategy is not None and child_strategy not in ("all", "any"):
            raise ValueError(f"Invalid child_strategy: {child_strategy!r}")
        cleaned_metric = metric_target.strip() if metric_target and metric_target.strip() else None
        cleaned_verify = verify_cmd.strip() if verify_cmd and verify_cmd.strip() else None
        if kind == "goal" and not cleaned_metric:
            raise ValueError("Goals require a non-empty metric_target.")

        with self._lock, self._conn:
            state = self.get_state()
            resolved_id = (task_id or "").strip() or self._next_task_id(kind)
            existing = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (resolved_id,)
            ).fetchone()

            if existing is not None and existing["status"] == "in_progress":
                if ready is not None:
                    raise ValueError(f"Cannot change status of in_progress task {resolved_id!r}.")
                status: TaskStatus = "in_progress"
            elif ready is True:
                status = "ready"
            elif ready is False:
                status = "draft"
            elif existing is not None:
                status = existing["status"]
            else:
                status = "ready" if state.mode == "AUTONOMOUS" else "draft"

            resolved_strategy: ChildStrategy = (
                child_strategy
                if child_strategy is not None
                else (existing["child_strategy"] if existing is not None else "all")
            )
            now = _iso(_utc_now())
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO tasks (
                        id, parent_id, kind, repo, title, spec, metric_target,
                        verify_cmd, status, priority, strike_count, child_strategy,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        resolved_id,
                        parent_id,
                        kind,
                        cleaned_repo,
                        title.strip(),
                        spec.strip(),
                        cleaned_metric,
                        cleaned_verify,
                        status,
                        int(priority),
                        resolved_strategy,
                        now,
                        now,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET parent_id = ?, kind = ?, repo = ?, title = ?, spec = ?,
                        metric_target = ?, verify_cmd = ?, status = ?, priority = ?,
                        child_strategy = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        parent_id if parent_id is not None else existing["parent_id"],
                        kind,
                        cleaned_repo,
                        title.strip(),
                        spec.strip(),
                        cleaned_metric if metric_target is not None else existing["metric_target"],
                        cleaned_verify if verify_cmd is not None else existing["verify_cmd"],
                        status,
                        int(priority),
                        resolved_strategy,
                        now,
                        resolved_id,
                    ),
                )
            return self.get_task(resolved_id)

    def get_task(self, task_id: str) -> TaskRow:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Task {task_id!r} not found")
            return self._row_to_task(row)

    def list_tasks(self, status: TaskStatus | None = None) -> list[TaskRow]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY priority ASC, created_at ASC",
                    (status,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tasks ORDER BY priority ASC, created_at ASC"
                ).fetchall()
            return [self._row_to_task(r) for r in rows]

    def reap_stale_leases(self, now: datetime | None = None) -> list[str]:
        now_dt = now or _utc_now()
        now_iso = _iso(now_dt)
        reaped: list[str] = []
        with self._lock, self._conn:
            rows = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'in_progress' AND lease_until IS NOT NULL AND lease_until < ?
                """,
                (now_iso,),
            ).fetchall()
            for row in rows:
                task_id = str(row["id"])
                reaped.append(task_id)
                role: WorkerRole = "builder" if row["verify_cmd"] else "planner"
                self._record_attempt_locked(
                    task_id=task_id,
                    role=role,
                    summary="Worker lease expired before completion; reaped by reconciler.",
                    outcome="reaped",
                    cost_usd=0.0,
                    now_iso=now_iso,
                )
        return reaped

    def claim_next_task(
        self, *, lease_seconds: int = 1200, task_id: str | None = None, now: datetime | None = None
    ) -> TaskRow | None:
        now_dt = now or _utc_now()
        now_iso = _iso(now_dt)
        lease_iso = _iso(now_dt + timedelta(seconds=lease_seconds))
        with self._lock, self._conn:
            busy_repos = {
                str(r["repo"])
                for r in self._conn.execute(
                    "SELECT repo FROM tasks WHERE status = 'in_progress'"
                ).fetchall()
            }
            if task_id is not None:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if (
                    row is None
                    or row["status"] in ("in_progress", "done")
                    or int(row["strike_count"]) >= 3
                    or str(row["repo"]) in busy_repos
                ):
                    return None
            else:
                row = self._conn.execute(
                    """
                    SELECT * FROM tasks
                    WHERE status = 'ready'
                      AND strike_count < 3
                      AND repo NOT IN (SELECT repo FROM tasks WHERE status = 'in_progress')
                    ORDER BY CASE WHEN kind = 'problem' THEN 0 ELSE 1 END ASC,
                             priority ASC,
                             created_at ASC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    return None

            claimed_id = str(row["id"])
            self._conn.execute(
                """
                UPDATE tasks
                SET status = 'in_progress', lease_until = ?, updated_at = ?
                WHERE id = ?
                """,
                (lease_iso, now_iso, claimed_id),
            )
            return self.get_task(claimed_id)

    def finish_attempt(
        self,
        *,
        task_id: str,
        role: WorkerRole,
        summary: str,
        outcome: AttemptOutcome,
        cost_usd: float = 0.0,
        pi_session_id: str | None = None,
        session_jsonl_path: str | None = None,
        git_branch: str | None = None,
        git_sha: str | None = None,
        verify_output: str | None = None,
        metric_value: float | None = None,
        skills_used: list[str] | None = None,
        diagnostic: str | None = None,
    ) -> TaskRow:
        now_iso = _iso(_utc_now())
        with self._lock, self._conn:
            if cost_usd > 0:
                self.add_spend(cost_usd)
            return self._record_attempt_locked(
                task_id=task_id,
                role=role,
                summary=summary,
                outcome=outcome,
                cost_usd=cost_usd,
                pi_session_id=pi_session_id,
                session_jsonl_path=session_jsonl_path,
                git_branch=git_branch,
                git_sha=git_sha,
                verify_output=verify_output,
                metric_value=metric_value,
                skills_used=skills_used,
                diagnostic=diagnostic,
                now_iso=now_iso,
            )

    def _record_attempt_locked(
        self,
        *,
        task_id: str,
        role: WorkerRole,
        summary: str,
        outcome: AttemptOutcome,
        cost_usd: float,
        now_iso: str,
        pi_session_id: str | None = None,
        session_jsonl_path: str | None = None,
        git_branch: str | None = None,
        git_sha: str | None = None,
        verify_output: str | None = None,
        metric_value: float | None = None,
        skills_used: list[str] | None = None,
        diagnostic: str | None = None,
    ) -> TaskRow:
        task = self.get_task(task_id)
        dedup_skills = list(dict.fromkeys(s.strip() for s in (skills_used or []) if s.strip()))
        self._conn.execute(
            """
            INSERT INTO attempts (
                task_id, role, pi_session_id, session_jsonl_path, summary,
                git_branch, git_sha, cost_usd, outcome, verify_output,
                metric_value, skills_used, diagnostic, reflected, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                task_id,
                role,
                pi_session_id,
                session_jsonl_path,
                summary,
                git_branch,
                git_sha,
                float(cost_usd),
                outcome,
                verify_output,
                float(metric_value) if metric_value is not None else None,
                json.dumps(dedup_skills),
                diagnostic.strip() if diagnostic and diagnostic.strip() else None,
                now_iso,
            ),
        )

        best_metric = task.best_metric
        best_branch = task.best_branch
        best_sha = task.best_sha

        if outcome == "pushed":
            new_status: TaskStatus = "done"
            strikes = task.strike_count
            if metric_value is not None and is_better_metric(
                task.metric_target, metric_value, best_metric
            ):
                best_metric = float(metric_value)
                best_branch = git_branch or best_branch
                best_sha = git_sha or best_sha
            elif best_branch is None and git_branch:
                best_branch = git_branch
                best_sha = git_sha
            metric_suffix = f" (metric={metric_value})" if metric_value is not None else ""
            self._queue_outbox_locked(
                kind="report",
                task_id=task_id,
                summary_spoken=(
                    f"Completed {task_id} ({task.title}) on branch "
                    f"{git_branch or f'brain/{task_id}'}{metric_suffix}."
                ),
                details={
                    "git_branch": git_branch,
                    "git_sha": git_sha,
                    "metric_value": metric_value,
                    "summary": summary,
                },
                now_iso=now_iso,
            )
        elif outcome == "decomposed":
            pending_children = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM tasks WHERE parent_id = ? AND status != 'done'",
                (task_id,),
            ).fetchone()
            has_pending = bool(pending_children and int(pending_children["cnt"]) > 0)
            new_status = "draft" if has_pending else "done"
            strikes = task.strike_count
        elif outcome == "deferred_approval":
            new_status = "draft"
            strikes = task.strike_count
        elif outcome == "reaped":
            new_status = "ready"
            strikes = task.strike_count
        else:
            strikes = task.strike_count + 1
            if strikes >= 3:
                new_status = "blocked"
                self._queue_outbox_locked(
                    kind="blocker",
                    task_id=task_id,
                    summary_spoken=f"Task {task_id} ({task.title}) is blocked after 3 failed attempts.",
                    details={"summary": summary, "verify_output": verify_output},
                    now_iso=now_iso,
                )
            else:
                new_status = "ready"

        self._conn.execute(
            """
            UPDATE tasks
            SET status = ?, strike_count = ?, best_metric = ?, best_branch = ?, best_sha = ?,
                lease_until = NULL, updated_at = ?
            WHERE id = ?
            """,
            (new_status, strikes, best_metric, best_branch, best_sha, now_iso, task_id),
        )
        if task.parent_id:
            parent = self.get_task(task.parent_id)
            parent_target = parent.metric_target or task.metric_target
            p_metric = parent.best_metric
            p_branch = parent.best_branch
            p_sha = parent.best_sha
            if outcome == "pushed":
                if metric_value is not None and is_better_metric(
                    parent_target, metric_value, p_metric
                ):
                    p_metric = float(metric_value)
                    p_branch = git_branch or p_branch
                    p_sha = git_sha or p_sha
                elif p_branch is None and git_branch:
                    p_branch = git_branch
                    p_sha = git_sha

            if parent.child_strategy == "any":
                met_target = outcome == "pushed" and (
                    (metric_value is not None and check_metric_target(parent_target, metric_value))
                    if has_metric_comparator(parent_target)
                    else True
                )
                if met_target:
                    self._conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'done', updated_at = ?
                        WHERE parent_id = ? AND status IN ('draft', 'ready')
                        """,
                        (now_iso, task.parent_id),
                    )
                    self._conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'done', best_metric = ?, best_branch = ?, best_sha = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (p_metric, p_branch, p_sha, now_iso, task.parent_id),
                    )
                else:
                    active_or_done = self._conn.execute(
                        """
                        SELECT COUNT(*) AS cnt FROM tasks
                        WHERE parent_id = ? AND status != 'blocked'
                        """,
                        (task.parent_id,),
                    ).fetchone()
                    all_blocked = active_or_done is not None and int(active_or_done["cnt"]) == 0
                    self._conn.execute(
                        """
                        UPDATE tasks
                        SET status = CASE WHEN ? THEN 'blocked' ELSE status END,
                            best_metric = ?, best_branch = ?, best_sha = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            1 if all_blocked else 0,
                            p_metric,
                            p_branch,
                            p_sha,
                            now_iso,
                            task.parent_id,
                        ),
                    )
            elif outcome == "pushed":
                rem = self._conn.execute(
                    "SELECT COUNT(*) AS cnt FROM tasks WHERE parent_id = ? AND status != 'done'",
                    (task.parent_id,),
                ).fetchone()
                parent_done = rem is not None and int(rem["cnt"]) == 0
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET status = CASE WHEN ? AND verify_cmd IS NULL THEN 'done' ELSE status END,
                        best_metric = ?, best_branch = ?, best_sha = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (1 if parent_done else 0, p_metric, p_branch, p_sha, now_iso, task.parent_id),
                )
        return self.get_task(task_id)

    def list_attempts(self, task_id: str | None = None) -> list[AttemptRow]:
        with self._lock:
            if task_id is not None:
                rows = self._conn.execute(
                    "SELECT * FROM attempts WHERE task_id = ? ORDER BY id ASC", (task_id,)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM attempts ORDER BY id ASC").fetchall()
            return [self._row_to_attempt(r) for r in rows]

    def list_unreflected_attempts(self, limit: int = 20) -> list[AttemptRow]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM attempts
                WHERE reflected = 0 AND outcome != 'reaped'
                ORDER BY id ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._row_to_attempt(r) for r in rows]

    def mark_attempts_reflected(self, attempt_ids: list[int]) -> None:
        if not attempt_ids:
            return
        placeholders = ",".join("?" for _ in attempt_ids)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE attempts SET reflected = 1 WHERE id IN ({placeholders})",
                tuple(int(i) for i in attempt_ids),
            )

    def get_skill(self, slug: str) -> SkillRow | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM skills WHERE slug = ?", (slug,)).fetchone()
            return self._row_to_skill(row) if row is not None else None

    def list_skills(self, status: SkillStatus | None = "active") -> list[SkillRow]:
        with self._lock:
            if status is not None:
                rows = self._conn.execute(
                    "SELECT * FROM skills WHERE status = ? ORDER BY slug ASC", (status,)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM skills ORDER BY slug ASC").fetchall()
            return [self._row_to_skill(r) for r in rows]

    def save_skill(
        self,
        *,
        slug: str,
        name: str,
        description: str,
        content: str,
        lesson: str,
        evidence_attempt_ids: list[int],
    ) -> SkillRow:
        now = _iso(_utc_now())
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        ev_json = json.dumps(sorted({int(i) for i in evidence_attempt_ids}))
        with self._lock, self._conn:
            existing = self.get_skill(slug)
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO skills (
                        slug, name, description, version, sha256, status, content,
                        previous_content, lesson, evidence_attempt_ids,
                        loads_since_update, successes_since_update, failures_since_update,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 1, ?, 'active', ?, NULL, ?, ?, 0, 0, 0, ?, ?)
                    """,
                    (slug, name, description, sha, content, lesson.strip(), ev_json, now, now),
                )
            else:
                prev_content = existing.content if existing.status == "active" else existing.previous_content
                next_ver = existing.version + 1
                self._conn.execute(
                    """
                    UPDATE skills
                    SET name = ?, description = ?, version = ?, sha256 = ?, status = 'active',
                        content = ?, previous_content = ?, lesson = ?, evidence_attempt_ids = ?,
                        loads_since_update = 0, successes_since_update = 0, failures_since_update = 0,
                        updated_at = ?
                    WHERE slug = ?
                    """,
                    (
                        name,
                        description,
                        next_ver,
                        sha,
                        content,
                        prev_content,
                        lesson.strip(),
                        ev_json,
                        now,
                        slug,
                    ),
                )
            saved = self.get_skill(slug)
            assert saved is not None
            return saved

    def retire_skill(self, slug: str) -> SkillRow:
        now = _iso(_utc_now())
        with self._lock, self._conn:
            existing = self.get_skill(slug)
            if existing is None:
                raise KeyError(f"Skill {slug!r} not found")
            self._conn.execute(
                "UPDATE skills SET status = 'retired', updated_at = ? WHERE slug = ?",
                (now, slug),
            )
            retired = self.get_skill(slug)
            assert retired is not None
            return retired

    def record_skill_usage(self, slugs: list[str], *, succeeded: bool) -> list[SkillRow]:
        """
        Records skill load outcome for each used skill. If a skill accumulates >= 2 failures
        with 0 successes since its creation/update, automatically rolls it back to `previous_content`
        (or retires it if no previous version exists) and returns the rolled-back SkillRows.
        """
        rolled_back: list[SkillRow] = []
        now = _iso(_utc_now())
        with self._lock, self._conn:
            for slug in dict.fromkeys(slugs):
                skill = self.get_skill(slug)
                if skill is None or skill.status != "active":
                    continue
                loads = skill.loads_since_update + 1
                succ = skill.successes_since_update + (1 if succeeded else 0)
                fail = skill.failures_since_update + (0 if succeeded else 1)
                if fail >= 2 and succ == 0:
                    if skill.previous_content:
                        prev_sha = hashlib.sha256(skill.previous_content.encode("utf-8")).hexdigest()
                        self._conn.execute(
                            """
                            UPDATE skills
                            SET content = previous_content, previous_content = NULL, sha256 = ?,
                                version = version + 1, loads_since_update = 0,
                                successes_since_update = 0, failures_since_update = 0, updated_at = ?
                            WHERE slug = ?
                            """,
                            (prev_sha, now, slug),
                        )
                    else:
                        self._conn.execute(
                            """
                            UPDATE skills
                            SET status = 'retired', loads_since_update = ?,
                                successes_since_update = ?, failures_since_update = ?, updated_at = ?
                            WHERE slug = ?
                            """,
                            (loads, succ, fail, now, slug),
                        )
                    updated = self.get_skill(slug)
                    assert updated is not None
                    rolled_back.append(updated)
                else:
                    self._conn.execute(
                        """
                        UPDATE skills
                        SET loads_since_update = ?, successes_since_update = ?,
                            failures_since_update = ?, updated_at = ?
                        WHERE slug = ?
                        """,
                        (loads, succ, fail, now, slug),
                    )
        return rolled_back

    def queue_outbox(
        self,
        *,
        kind: OutboxKind,
        summary_spoken: str,
        task_id: str | None = None,
        resume_session_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> OutboxRow:
        with self._lock, self._conn:
            return self._queue_outbox_locked(
                kind=kind,
                summary_spoken=summary_spoken,
                task_id=task_id,
                resume_session_id=resume_session_id,
                details=details or {},
                now_iso=_iso(_utc_now()),
            )

    def _queue_outbox_locked(
        self,
        *,
        kind: OutboxKind,
        summary_spoken: str,
        now_iso: str,
        task_id: str | None = None,
        resume_session_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> OutboxRow:
        outbox_id = f"OB-{uuid.uuid4().hex[:8]}"
        self._conn.execute(
            """
            INSERT INTO outbox (id, kind, task_id, resume_session_id, summary_spoken, details_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
            """,
            (
                outbox_id,
                kind,
                task_id,
                resume_session_id,
                summary_spoken.strip(),
                json.dumps(details or {}),
                now_iso,
            ),
        )
        row = self._conn.execute("SELECT * FROM outbox WHERE id = ?", (outbox_id,)).fetchone()
        assert row is not None
        return self._row_to_outbox(row)

    def drain_digest(self) -> tuple[list[OutboxRow], OutboxRow | None]:
        """Marks queued non-approval items as spoken and returns them plus the oldest pending approval."""
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM outbox WHERE status = 'queued' ORDER BY created_at ASC"
            ).fetchall()
            items = [self._row_to_outbox(r) for r in rows]
            reports = [item for item in items if item.kind != "approval_request"]
            approvals = [item for item in items if item.kind == "approval_request"]
            for item in reports:
                self._conn.execute(
                    "UPDATE outbox SET status = 'spoken' WHERE id = ?", (item.id,)
                )
            return reports, (approvals[0] if approvals else None)

    def resolve_approval(self, outbox_id: str, approved: bool) -> OutboxRow | None:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            if row is None:
                return None
            new_status: OutboxStatus = "approved" if approved else "rejected"
            self._conn.execute(
                "UPDATE outbox SET status = ? WHERE id = ?", (new_status, outbox_id)
            )
            if row["task_id"] and approved:
                self._conn.execute(
                    "UPDATE tasks SET status = 'ready', updated_at = ? WHERE id = ?",
                    (_iso(_utc_now()), row["task_id"]),
                )
            updated = self._conn.execute(
                "SELECT * FROM outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            assert updated is not None
            return self._row_to_outbox(updated)

    def get_unconsumed_approved_outbox(self, task_id: str) -> OutboxRow | None:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM outbox
                WHERE task_id = ? AND kind = 'approval_request' AND status = 'approved'
                ORDER BY created_at DESC
                """,
                (task_id,),
            ).fetchall()
            for row in rows:
                try:
                    details = json.loads(row["details_json"] or "{}")
                except Exception:
                    details = {}
                if not details.get("consumed"):
                    return self._row_to_outbox(row)
            return None

    def mark_outbox_consumed(self, outbox_id: str) -> None:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT details_json FROM outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            if row is None:
                return
            try:
                details = json.loads(row["details_json"] or "{}")
            except Exception:
                details = {}
            details["consumed"] = True
            self._conn.execute(
                "UPDATE outbox SET details_json = ? WHERE id = ?",
                (json.dumps(details), outbox_id),
            )

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> TaskRow:
        keys = set(row.keys())
        return TaskRow(
            id=str(row["id"]),
            parent_id=row["parent_id"],
            kind=row["kind"],
            repo=str(row["repo"]),
            title=str(row["title"]),
            spec=str(row["spec"]),
            metric_target=row["metric_target"],
            verify_cmd=row["verify_cmd"],
            status=row["status"],
            priority=int(row["priority"]),
            strike_count=int(row["strike_count"]),
            lease_until=row["lease_until"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            child_strategy=(
                row["child_strategy"]
                if "child_strategy" in keys and row["child_strategy"] in ("all", "any")
                else "all"
            ),
            best_metric=(
                float(row["best_metric"])
                if "best_metric" in keys and row["best_metric"] is not None
                else None
            ),
            best_branch=row["best_branch"] if "best_branch" in keys else None,
            best_sha=row["best_sha"] if "best_sha" in keys else None,
        )

    @staticmethod
    def _row_to_attempt(row: sqlite3.Row) -> AttemptRow:
        keys = set(row.keys())
        raw_skills = row["skills_used"] if "skills_used" in keys else "[]"
        try:
            parsed_skills = tuple(str(x) for x in json.loads(raw_skills or "[]"))
        except Exception:
            parsed_skills = ()
        return AttemptRow(
            id=int(row["id"]),
            task_id=str(row["task_id"]),
            role=row["role"],
            pi_session_id=row["pi_session_id"],
            session_jsonl_path=row["session_jsonl_path"],
            summary=str(row["summary"]),
            git_branch=row["git_branch"],
            git_sha=row["git_sha"],
            cost_usd=float(row["cost_usd"]),
            outcome=row["outcome"],
            verify_output=row["verify_output"],
            created_at=str(row["created_at"]),
            metric_value=(
                float(row["metric_value"])
                if "metric_value" in keys and row["metric_value"] is not None
                else None
            ),
            skills_used=parsed_skills,
            diagnostic=row["diagnostic"] if "diagnostic" in keys else None,
            reflected=bool(row["reflected"]) if "reflected" in keys else False,
        )

    @staticmethod
    def _row_to_skill(row: sqlite3.Row) -> SkillRow:
        try:
            ev_ids = tuple(int(x) for x in json.loads(row["evidence_attempt_ids"] or "[]"))
        except Exception:
            ev_ids = ()
        return SkillRow(
            slug=str(row["slug"]),
            name=str(row["name"]),
            description=str(row["description"]),
            version=int(row["version"]),
            sha256=str(row["sha256"]),
            status=row["status"],
            content=str(row["content"]),
            previous_content=row["previous_content"],
            lesson=str(row["lesson"]),
            evidence_attempt_ids=ev_ids,
            loads_since_update=int(row["loads_since_update"]),
            successes_since_update=int(row["successes_since_update"]),
            failures_since_update=int(row["failures_since_update"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_outbox(row: sqlite3.Row) -> OutboxRow:
        return OutboxRow(
            id=str(row["id"]),
            kind=row["kind"],
            task_id=row["task_id"],
            resume_session_id=row["resume_session_id"],
            summary_spoken=str(row["summary_spoken"]),
            details_json=str(row["details_json"]),
            status=row["status"],
            created_at=str(row["created_at"]),
        )
