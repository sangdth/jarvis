from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from superbrain.config import Settings, settings
from superbrain.db import AttemptOutcome, BrainDB, TaskRow, WorkerRole, check_metric_target
from superbrain.sandbox import wrap_with_sandbox
from superbrain.skills import (
    SkillProposal,
    apply_skill_proposal,
    inspect_attempt_trajectory,
    record_skill_outcome,
    sync_skills_on_disk,
    validate_evidence_receipt,
    write_attempt_trace,
)

ApprovalCallback = Callable[[str, str, str | None], Awaitable[bool | None]]

PLANNER_TOOLS = (
    "read,grep,find,ls,research_scout,upsert_task,report_diagnostic,defer_for_approval"
)
BUILDER_TOOLS = (
    "read,edit,write,edit_and_run,bash,grep,find,ls,research_scout,"
    "record_note,report_diagnostic,defer_for_approval"
)
IMPROVER_TOOLS = "read,grep,find,ls,research_scout,propose_skill_change"
SCOUT_TOOLS = "read,grep,find,ls"
_METRIC_LINE_RE = re.compile(r"\bMETRIC\s*[:=]\s*(-?\d+(?:\.\d+)?)\b", re.IGNORECASE)
PROTECTED_SELF_PATHS = {
    "pyproject.toml",
    "src/superbrain/config.py",
    "src/superbrain/sandbox.py",
    "src/superbrain/defaults/pi/board.ts",
}


def extract_metric_value(output: str | None) -> float | None:
    """Extracts the last `METRIC: <float>` or `METRIC=<float>` reported by `verify_cmd`."""
    if not output:
        return None
    matches = _METRIC_LINE_RE.findall(output)
    return float(matches[-1]) if matches else None


@dataclass
class PiRunResult:
    outcome: AttemptOutcome
    summary: str
    cost_usd: float
    session_id: str | None = None
    session_jsonl_path: str | None = None
    upserted_task_ids: list[str] | None = None
    skills_used: list[str] | None = None
    diagnostic: str | None = None
    modified_skills: list[str] | None = None


def extract_event_cost(event: dict[str, Any]) -> float:
    """Extracts incremental or total USD cost from a `pi --mode json` event if present."""
    if isinstance(event.get("cost_usd"), (int, float)):
        return float(event["cost_usd"])
    usage = event.get("usage")
    if isinstance(usage, dict):
        if isinstance(usage.get("cost_usd"), (int, float)):
            return float(usage["cost_usd"])
        cost = usage.get("cost")
        if isinstance(cost, (int, float)):
            return float(cost)
        if isinstance(cost, dict) and isinstance(cost.get("total"), (int, float)):
            return float(cost["total"])
    msg = event.get("message")
    if isinstance(msg, dict):
        return extract_event_cost(msg)
    return 0.0


def extract_assistant_text(event: dict[str, Any]) -> str | None:
    """Extracts assistant text from a `pi --mode json` event."""
    msg = event.get("message") if isinstance(event.get("message"), dict) else event
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return None
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(item.get("text", "")).strip()
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
        ]
        joined = "\n".join(p for p in parts if p)
        return joined if joined else None
    return None


class Reconciler:
    """Deterministic goal and problem reconciler driving ephemeral `pi 1.5.0` workers."""

    def __init__(
        self,
        db: BrainDB,
        cfg: Settings = settings,
        on_restart_requested: Callable[[], None] | None = None,
    ) -> None:
        self.db = db
        self.cfg = cfg
        self.on_restart_requested = on_restart_requested
        self.restart_pending: bool = False
        self.cfg.worktrees_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cfg.sessions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cfg.notes_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cfg.skills_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cfg.traces_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        sync_skills_on_disk(self.db, self.cfg.skills_dir)
        self._board_ext_path = self._install_board_extension()
        self._loop_task: asyncio.Task[None] | None = None
        self._active_procs: dict[str, asyncio.subprocess.Process] = {}

    def _install_board_extension(self) -> Path:
        ext_dir = self.cfg.home_dir / "ext"
        ext_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = ext_dir / "board.ts"
        source_text = (
            files("superbrain")
            .joinpath("defaults/pi/board.ts")
            .read_text(encoding="utf-8")
        )
        target.write_text(source_text, encoding="utf-8")
        os.chmod(target, 0o600)
        return target

    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        for proc in list(self._active_procs.values()):
            if proc.returncode is None:
                proc.terminate()

    def abort_run(self, run_key: str) -> bool:
        proc = self._active_procs.get(run_key)
        if proc and proc.returncode is None:
            proc.terminate()
            return True
        return False

    async def _run_loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[Reconciler] tick error: {exc!r}")
            await asyncio.sleep(self.cfg.reconcile_interval_seconds)

    async def tick(self) -> TaskRow | None:
        """Executes one deterministic reconciler cycle."""
        self.db.reap_stale_leases()
        state = self.db.get_state()
        if state.mode != "AUTONOMOUS":
            return None
        if state.spent_today_usd >= state.daily_budget_usd:
            logger.warning("[Reconciler] Daily budget reached; pausing autonomous claiming.")
            return None

        task = self.db.claim_next_task(lease_seconds=self.cfg.lease_seconds)
        if task is None:
            unreflected = self.db.list_unreflected_attempts()
            should_reflect = len(unreflected) >= 2 or any(
                a.outcome in ("failed_verify", "errored", "timed_out") or bool(a.diagnostic)
                for a in unreflected
            )
            if should_reflect:
                await self.run_skill_improver()
            return None

        finished = await self.execute_claimed_task(task)
        if self.restart_pending and self.on_restart_requested is not None:
            self.on_restart_requested()
        return finished

    async def execute_claimed_task(
        self,
        task: TaskRow,
        *,
        run_key: str | None = None,
        resume_session_id: str | None = None,
        approval_cb: ApprovalCallback | None = None,
    ) -> TaskRow:
        role: WorkerRole = "builder" if task.verify_cmd else "planner"
        effective_resume_id = resume_session_id or self.db.get_approved_resume_session(task.id)
        repo_dir = (self.cfg.projects_dir / self.db.validate_repo(task.repo)).resolve()
        if not (repo_dir / ".git").exists():
            return self.db.finish_attempt(
                task_id=task.id,
                role=role,
                summary=f"Repository {task.repo!r} not found at {repo_dir}",
                outcome="errored",
            )

        session_dir = self.cfg.sessions_dir / task.id
        session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        if role == "planner":
            try:
                await self._git_fetch(repo_dir)
                prompt = self._build_planner_prompt(task)
                pi_res = await self._run_pi(
                    role="planner",
                    cwd=repo_dir,
                    worktree_dir=None,
                    session_dir=session_dir,
                    task=task,
                    prompt=prompt,
                    run_key=run_key or task.id,
                    resume_session_id=effective_resume_id,
                    approval_cb=approval_cb,
                )
                outcome: AttemptOutcome = (
                    "decomposed" if pi_res.outcome == "pushed" else pi_res.outcome
                )
                finished_task = self.db.finish_attempt(
                    task_id=task.id,
                    role="planner",
                    summary=pi_res.summary,
                    outcome=outcome,
                    cost_usd=pi_res.cost_usd,
                    pi_session_id=pi_res.session_id,
                    session_jsonl_path=pi_res.session_jsonl_path,
                    skills_used=pi_res.skills_used,
                    diagnostic=pi_res.diagnostic,
                )
                self._finalize_attempt_artifacts(task.id)
                succeeded = outcome == "decomposed"
                record_skill_outcome(
                    self.db,
                    self.cfg.skills_dir,
                    pi_res.skills_used or [],
                    succeeded=succeeded,
                )
                if outcome != "deferred_approval":
                    await self._check_post_upgrade_health(succeeded=succeeded)
                return finished_task
            except Exception as exc:
                logger.error(f"[Reconciler] planner task {task.id} failed: {exc!r}")
                finished_task = self.db.finish_attempt(
                    task_id=task.id,
                    role="planner",
                    summary=f"Planner execution error: {exc}",
                    outcome="errored",
                )
                self._finalize_attempt_artifacts(task.id)
                await self._check_post_upgrade_health(succeeded=False)
                return finished_task

        worktree_dir = self.cfg.worktrees_dir / task.id
        branch_name = f"brain/{task.id}"
        try:
            await self._git_fetch(repo_dir)
            base_sha = await self._prepare_worktree(repo_dir, worktree_dir, branch_name)
            prompt = self._build_builder_prompt(task, worktree_dir)
            pi_res = await self._run_pi(
                role="builder",
                cwd=worktree_dir,
                worktree_dir=worktree_dir,
                session_dir=session_dir,
                task=task,
                prompt=prompt,
                run_key=run_key or task.id,
                resume_session_id=effective_resume_id,
                approval_cb=approval_cb,
            )
            diff_stat = await self._worktree_diff_stat(worktree_dir, base_sha)
            if pi_res.outcome != "pushed":
                finished_task = self.db.finish_attempt(
                    task_id=task.id,
                    role="builder",
                    summary=pi_res.summary,
                    outcome=pi_res.outcome,
                    cost_usd=pi_res.cost_usd,
                    pi_session_id=pi_res.session_id,
                    session_jsonl_path=pi_res.session_jsonl_path,
                    git_branch=branch_name,
                    skills_used=pi_res.skills_used,
                    diagnostic=pi_res.diagnostic,
                )
                self._finalize_attempt_artifacts(task.id, diff_stat=diff_stat)
                if pi_res.outcome != "deferred_approval":
                    record_skill_outcome(
                        self.db, self.cfg.skills_dir, pi_res.skills_used or [], succeeded=False
                    )
                    await self._check_post_upgrade_health(succeeded=False)
                return finished_task

            if task.kind == "problem":
                deleted_tests = await self._find_deleted_tests(worktree_dir, base_sha)
                if deleted_tests:
                    finished_task = self.db.finish_attempt(
                        task_id=task.id,
                        role="builder",
                        summary=pi_res.summary,
                        outcome="failed_verify",
                        cost_usd=pi_res.cost_usd,
                        pi_session_id=pi_res.session_id,
                        session_jsonl_path=pi_res.session_jsonl_path,
                        git_branch=branch_name,
                        verify_output=f"Blocked: problem task deleted test files: {', '.join(deleted_tests)}",
                        skills_used=pi_res.skills_used,
                        diagnostic=pi_res.diagnostic,
                    )
                    self._finalize_attempt_artifacts(task.id, diff_stat=diff_stat)
                    record_skill_outcome(
                        self.db, self.cfg.skills_dir, pi_res.skills_used or [], succeeded=False
                    )
                    await self._check_post_upgrade_health(succeeded=False)
                    return finished_task

            assert task.verify_cmd is not None
            effective_verify_cmd = task.verify_cmd
            if task.repo == "superbrain" and self.cfg.self_upgrade_verify_cmd.strip():
                effective_verify_cmd = (
                    f"({task.verify_cmd}) && ({self.cfg.self_upgrade_verify_cmd})"
                )
            verified, verify_output = await self._run_verify(
                worktree_dir, session_dir, effective_verify_cmd
            )
            metric_val = extract_metric_value(verify_output)
            if (
                verified
                and metric_val is not None
                and not check_metric_target(task.metric_target, metric_val)
            ):
                verified = False
                verify_output = (
                    f"{verify_output}\n"
                    f"Metric {metric_val} did not satisfy metric_target {task.metric_target!r}"
                ).strip()
            if verified and task.repo == "superbrain" and not self.db.has_approved_outbox(task.id):
                verified, baseline_out = await self._run_held_out_baseline_tests(
                    repo_dir, worktree_dir, session_dir
                )
                if not verified:
                    verify_output = (
                        "Held-out baseline tests failed (details sealed to prevent overfitting)."
                    )
                    self.db.queue_outbox(
                        kind="report",
                        task_id=task.id,
                        summary_spoken=f"Task {task.id} failed held-out baseline tests.",
                        details={"held_out_output": baseline_out},
                    )
            if not verified:
                finished_task = self.db.finish_attempt(
                    task_id=task.id,
                    role="builder",
                    summary=pi_res.summary,
                    outcome="failed_verify",
                    cost_usd=pi_res.cost_usd,
                    pi_session_id=pi_res.session_id,
                    session_jsonl_path=pi_res.session_jsonl_path,
                    git_branch=branch_name,
                    verify_output=verify_output,
                    metric_value=metric_val,
                    skills_used=pi_res.skills_used,
                    diagnostic=pi_res.diagnostic,
                )
                self._finalize_attempt_artifacts(task.id, diff_stat=diff_stat)
                record_skill_outcome(
                    self.db, self.cfg.skills_dir, pi_res.skills_used or [], succeeded=False
                )
                await self._check_post_upgrade_health(succeeded=False)
                if (
                    self.cfg.auto_research_after_strikes > 0
                    and finished_task.strike_count == self.cfg.auto_research_after_strikes
                    and finished_task.parent_id is None
                    and not any(t.parent_id == finished_task.id for t in self.db.list_tasks())
                ):
                    await self.run_auto_research(finished_task.id)
                    finished_task = self.db.get_task(finished_task.id)
                return finished_task

            if task.repo == "superbrain":
                touched_protected = await self._find_modified_protected_files(
                    worktree_dir, base_sha
                )
                if touched_protected and not self.db.has_approved_outbox(task.id):
                    prompt_spoken = (
                        f"Approve Superbrain self-upgrade modifying protected files "
                        f"({', '.join(touched_protected)})?"
                    )
                    approved: bool | None = None
                    if approval_cb is not None and self.db.get_state().mode == "ASSIST":
                        approved = await approval_cb(
                            prompt_spoken, pi_res.summary, pi_res.session_id
                        )
                    if not approved:
                        if approved is None:
                            self.db.queue_outbox(
                                kind="approval_request",
                                task_id=task.id,
                                resume_session_id=pi_res.session_id,
                                summary_spoken=prompt_spoken,
                                details={"files": touched_protected, "summary": pi_res.summary},
                            )
                        finished_task = self.db.finish_attempt(
                            task_id=task.id,
                            role="builder",
                            summary=f"Awaiting owner approval for protected files: {', '.join(touched_protected)}",
                            outcome="deferred_approval",
                            cost_usd=pi_res.cost_usd,
                            pi_session_id=pi_res.session_id,
                            session_jsonl_path=pi_res.session_jsonl_path,
                            git_branch=branch_name,
                            skills_used=pi_res.skills_used,
                            diagnostic=pi_res.diagnostic,
                        )
                        self._finalize_attempt_artifacts(task.id, diff_stat=diff_stat)
                        return finished_task

            git_sha = await self._commit_and_push(worktree_dir, branch_name, task)
            just_upgraded_self = False
            if task.repo == "superbrain":
                await self._apply_self_upgrade(repo_dir, git_sha, task.id)
                just_upgraded_self = True
            finished_task = self.db.finish_attempt(
                task_id=task.id,
                role="builder",
                summary=pi_res.summary,
                outcome="pushed",
                cost_usd=pi_res.cost_usd,
                pi_session_id=pi_res.session_id,
                session_jsonl_path=pi_res.session_jsonl_path,
                git_branch=branch_name,
                git_sha=git_sha,
                verify_output=verify_output,
                metric_value=metric_val,
                skills_used=pi_res.skills_used,
                diagnostic=pi_res.diagnostic,
            )
            self._finalize_attempt_artifacts(task.id, diff_stat=diff_stat)
            record_skill_outcome(
                self.db, self.cfg.skills_dir, pi_res.skills_used or [], succeeded=True
            )
            if not just_upgraded_self:
                await self._check_post_upgrade_health(succeeded=True)
            return finished_task
        except Exception as exc:
            logger.error(f"[Reconciler] builder task {task.id} failed: {exc!r}")
            finished_task = self.db.finish_attempt(
                task_id=task.id,
                role="builder",
                summary=f"Builder execution error: {exc}",
                outcome="errored",
                git_branch=branch_name,
            )
            self._finalize_attempt_artifacts(task.id)
            await self._check_post_upgrade_health(succeeded=False)
            return finished_task
        finally:
            await self._cleanup_worktree(repo_dir, worktree_dir)

    async def run_interactive_planner(
        self,
        *,
        user_input: str,
        run_key: str,
        resume_session_id: str | None = None,
        approval_cb: ApprovalCallback | None = None,
    ) -> PiRunResult:
        """Runs a daytime/interactive Planner turn from voice input (`POST /v1/runs` or `/v1/ask`)."""
        session_dir = self.cfg.sessions_dir / "interactive"
        session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        allowed = sorted(self.db.allowed_repos)
        default_repo = allowed[0] if allowed else ""
        cwd = (
            (self.cfg.projects_dir / default_repo).resolve()
            if default_repo and (self.cfg.projects_dir / default_repo).is_dir()
            else self.cfg.home_dir
        )
        tasks_summary = "\n".join(
            f"- {t.id} [{t.kind}/{t.status}] ({t.repo}): {t.title} (verify={t.verify_cmd!r})"
            for t in self.db.list_tasks()[:20]
        )
        prompt = (
            f"You are Superbrain Planner running in {self.db.get_state().mode} mode.\n"
            f"Allowed repositories: {allowed}\n"
            f"Current tasks on board:\n{tasks_summary or '(none)'}\n\n"
            f"Owner request: {user_input}\n"
            "Use `upsert_task` to create or update tasks when appropriate. "
            "If the owner complains about a Superbrain bug/feature or asks to upgrade Superbrain "
            "itself ('the brain', 'your code', 'superbrain'), call `upsert_task` with "
            "`repo='superbrain'`, `kind='problem'` (for bugs/complaints) or `kind='goal'` "
            "(for new tech/upgrades), a deterministic `verify_cmd` (e.g. 'uv run pytest'), and "
            "`ready=true` so Superbrain immediately builds, verifies, and restarts itself. "
            "Reply in 1-2 concise sentences suitable to be spoken aloud."
        )
        res = await self._run_pi(
            role="planner",
            cwd=cwd,
            worktree_dir=None,
            session_dir=session_dir,
            task=None,
            prompt=prompt,
            run_key=run_key,
            resume_session_id=resume_session_id,
            approval_cb=approval_cb,
        )
        if res.cost_usd > 0:
            self.db.add_spend(res.cost_usd)
        return res

    async def run_skill_improver(self, *, run_key: str = "improver") -> PiRunResult:
        """
        Runs the Perplexity-style 4-stage self-improvement reflection loop over unreflected attempts:
        1. Evidence: compact trajectory summaries + skill load / failure stats + diagnostics
        2. Reflection: offline `improver` agent proposes at most 1 skill create/update/retire
        3. Verification & 4. Activation: enforced deterministically in `propose_skill_change` RPC
        """
        unreflected = self.db.list_unreflected_attempts(limit=20)
        if not unreflected:
            return PiRunResult(
                outcome="pushed",
                summary="No unreflected attempts available for skill improvement.",
                cost_usd=0.0,
                modified_skills=[],
            )

        session_dir = self.cfg.sessions_dir / "improver"
        session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        attempt_index_lines: list[str] = []
        for a in unreflected:
            trace_file = self.cfg.traces_dir / f"attempt-{a.id}.json"
            if not trace_file.is_file():
                write_attempt_trace(self.cfg.traces_dir, a)
            attempt_index_lines.append(
                f"- Attempt #{a.id} [{a.role}/{a.outcome}] task={a.task_id} "
                f"skills={list(a.skills_used)} diag={a.diagnostic!r} "
                f"trace={trace_file}"
            )

        active_skills = self.db.list_skills(status="active")
        skill_index_lines = [
            f"- {s.slug} (v{s.version}, sha256={s.sha256}, loads={s.loads_since_update}, "
            f"ok={s.successes_since_update}, fail={s.failures_since_update}): "
            f"{s.description} | path={self.cfg.skills_dir / s.slug / 'SKILL.md'}"
            for s in active_skills
        ]
        prompt = (
            "You are Superbrain Skill Improver. Use `read` and `grep` to inspect the trace files "
            "and skill files indexed below before deciding whether to propose an improvement.\n"
            "Rules:\n"
            "- Creating a new skill (`action='create'`) requires >= 2 independent attempt IDs showing "
            "the same recurring procedure or pitfall.\n"
            "- Prefer `action='append'` (with `expected_sha256`) to add an itemized rule bullet to an "
            "existing skill without rewriting or dropping its existing instructions; use `action='update'` "
            "only when refactoring conflicting lines.\n"
            f"- If active skills == {self.cfg.max_personal_skills}, creating a skill requires "
            "`retire_slug` and `retire_reason`.\n"
            "- Propose at most ONE change via `propose_skill_change`, or abstain if the evidence "
            "does not justify a change.\n\n"
            f"Active skills ({len(active_skills)}/{self.cfg.max_personal_skills}):\n"
            f"{chr(10).join(skill_index_lines) or '(none)'}\n\n"
            f"Unreflected attempt traces ({len(unreflected)}):\n"
            f"{chr(10).join(attempt_index_lines)}"
        )
        res = await self._run_pi(
            role="improver",
            cwd=self.cfg.home_dir,
            worktree_dir=None,
            session_dir=session_dir,
            task=None,
            prompt=prompt,
            run_key=run_key,
        )
        if res.cost_usd > 0:
            self.db.add_spend(res.cost_usd)
        if res.outcome != "errored":
            self.db.mark_attempts_reflected([a.id for a in unreflected])
        return res

    async def run_auto_research(self, task_id: str, *, run_key: str | None = None) -> PiRunResult:
        """
        Runs a SoL-Pi style Auto-Research Loop (Oracle Analysis + Breadth-First Hypothesis Fan-Out)
        for `task_id`: inspects past attempt trajectories and notes, optionally runs `research_scout`
        for byte-verified evidence, and fans out 2-3 competing child hypothesis tasks (`SG-<id>.1`,
        `SG-<id>.2`, ...) evaluated under the parent's verification and metric gates.
        """
        task = self.db.get_task(task_id)
        repo_dir = (self.cfg.projects_dir / self.db.validate_repo(task.repo)).resolve()
        cwd = repo_dir if repo_dir.is_dir() else self.cfg.home_dir
        session_dir = self.cfg.sessions_dir / f"{task.id}-research"
        session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        attempts = self.db.list_attempts(task.id)
        oracle_lines: list[str] = []
        for a in attempts[-3:]:
            rec = inspect_attempt_trajectory(a)
            oracle = rec.get("oracle") or {}
            oracle_lines.append(
                f"- Attempt #{a.id} ({a.outcome}, metric={a.metric_value}): {a.summary} | "
                f"turns={oracle.get('assistant_turns', 0)} "
                f"edit->bash={oracle.get('adjacent_edit_bash_pairs', 0)} "
                f"repeated_err_tools={oracle.get('repeated_error_tools', [])} | "
                f"verify={(a.verify_output or '')[:300]}"
            )
        note_file = self.cfg.notes_dir / f"{task.id}.md"
        notes = note_file.read_text(encoding="utf-8") if note_file.exists() else ""

        prompt = (
            f"You are Superbrain Auto-Research Planner (SoL-Pi Oracle + Breadth-First Hypothesis Search).\n"
            f"Target task: {task.id} [{task.kind}] in repo {task.repo!r}: {task.title}\n"
            f"Spec: {task.spec}\n"
            f"Metric target: {task.metric_target or 'pass verify_cmd'}\n"
            f"Verification command: {task.verify_cmd or '(to be defined per child hypothesis)'}\n"
            f"Oracle trajectory analysis:\n{chr(10).join(oracle_lines) or '(no prior attempts)'}\n"
            f"Negative-result ledger:\n{notes or '(none)'}\n\n"
            "Instructions:\n"
            "1. Use `research_scout` if needed to gather byte-verified quotes from the repository.\n"
            "2. Instead of repeating a failed single-lineage approach, fan out 2-3 distinct competing "
            f"hypothesis child tasks via `upsert_task` with `parent_id={task.id!r}`, `repo={task.repo!r}`, "
            "a concrete hypothesis in `spec`, a deterministic `verify_cmd` (printing `METRIC: <number>` "
            "when optimizing a numeric metric), `metric_target`, and `ready=true`.\n"
            "3. Summarize the competing hypotheses in 1-2 sentences."
        )
        res = await self._run_pi(
            role="planner",
            cwd=cwd,
            worktree_dir=None,
            session_dir=session_dir,
            task=task,
            prompt=prompt,
            run_key=run_key or f"research-{task.id}",
        )
        if res.cost_usd > 0:
            self.db.add_spend(res.cost_usd)
        if res.upserted_task_ids:
            self.db.upsert_task(
                task_id=task.id,
                parent_id=task.parent_id,
                kind=task.kind,
                repo=task.repo,
                title=task.title,
                spec=task.spec,
                metric_target=task.metric_target,
                verify_cmd=task.verify_cmd,
                priority=task.priority,
                ready=False,
            )
        return res

    def _build_planner_prompt(self, task: TaskRow) -> str:
        return (
            f"Decompose task {task.id} ({task.title}) in repo {task.repo!r} into 1-3 atomic child tasks "
            f"(or 2-3 competing auto-research hypothesis tasks when exploring an optimization or uncertain root cause) "
            f"using `upsert_task` with `parent_id={task.id!r}`, each with a deterministic `verify_cmd` "
            f"(which may print `METRIC: <float>` for quantitative goals), `metric_target`, and `ready=true`. "
            f"Use `research_scout` first when you need byte-verified codebase evidence.\nSpec: {task.spec}"
        )

    def _build_builder_prompt(self, task: TaskRow, worktree_dir: Path) -> str:
        attempts = self.db.list_attempts(task.id)
        history = "\n".join(
            f"- Attempt {a.id} ({a.outcome}, metric={a.metric_value}): "
            f"{a.summary} | verify_output: {(a.verify_output or '')[:400]}"
            for a in attempts[-3:]
        )
        note_file = self.cfg.notes_dir / f"{task.id}.md"
        notes = note_file.read_text(encoding="utf-8") if note_file.exists() else ""
        anti_repeat = (
            "Do NOT repeat a previously failed approach recorded above; diagnose the causal "
            "difference and take a distinct implementation path.\n"
            if attempts
            else ""
        )
        return (
            f"Task {task.id} ({task.kind}): {task.title}\n"
            f"Worktree: {worktree_dir}\n"
            f"Spec: {task.spec}\n"
            f"Metric target: {task.metric_target or '(pass verification)'}\n"
            f"Verification gate (`verify_cmd`): {task.verify_cmd}\n"
            f"Previous attempts:\n{history or '(none)'}\n"
            f"Progress & negative-result notes:\n{notes or '(none)'}\n\n"
            f"{anti_repeat}"
            "Prefer `research_scout` to delegate read-only reconnaissance with byte-verified quotes, "
            "and `edit_and_run` (Action Fusion) to fuse file edits with immediate test execution in one turn. "
            "Implement the minimal complete fix or feature inside this worktree, verify it, and "
            "summarize your changes in one sentence."
        )

    async def _run_pi(
        self,
        *,
        role: WorkerRole,
        cwd: Path,
        worktree_dir: Path | None,
        session_dir: Path,
        task: TaskRow | None,
        prompt: str,
        run_key: str,
        resume_session_id: str | None = None,
        approval_cb: ApprovalCallback | None = None,
    ) -> PiRunResult:
        sock_path = Path(f"/tmp/sb-{uuid.uuid4().hex[:12]}.sock")
        deferred_flag = False
        upserted_ids: list[str] = []
        skills_loaded: list[str] = []
        diagnostics: list[str] = []
        modified_skills: list[str] = []
        completed_cost = 0.0
        streaming_cost = 0.0
        total_cost = 0.0
        timed_out = False
        budget_exceeded = False
        initial_state = self.db.get_state()

        async def handle_rpc(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal deferred_flag, completed_cost, total_cost, budget_exceeded
            try:
                line = await reader.readline()
                if not line:
                    return
                req = json.loads(line.decode("utf-8"))
                method = req.get("method")
                params = req.get("params") or {}
                if method == "upsert_task":
                    if role != "planner":
                        raise ValueError("upsert_task is only permitted for the planner role.")
                    t = self.db.upsert_task(
                        task_id=params.get("id"),
                        parent_id=params.get("parent_id") or (task.id if task else None),
                        kind=params["kind"],
                        repo=params["repo"],
                        title=params["title"],
                        spec=params["spec"],
                        metric_target=params.get("metric_target"),
                        verify_cmd=params.get("verify_cmd"),
                        priority=int(params.get("priority", 50)),
                        ready=params.get("ready"),
                    )
                    upserted_ids.append(t.id)
                    resp: dict[str, Any] = {"ok": True, "result": {"id": t.id, "status": t.status}}
                elif method == "record_note":
                    target_id = task.id if task else str(params.get("task_id") or "general")
                    if not re.fullmatch(r"[A-Za-z0-9_.-]+", target_id) or ".." in target_id:
                        raise ValueError(f"Invalid note task_id: {target_id!r}")
                    note_path = self.cfg.notes_dir / f"{target_id}.md"
                    with note_path.open("a", encoding="utf-8") as f:
                        f.write(f"\n{params.get('note_markdown', '').strip()}\n")
                    os.chmod(note_path, 0o600)
                    resp = {"ok": True, "result": {"saved": str(note_path)}}
                elif method == "record_skill_load":
                    slug = str(params.get("slug") or "").strip()
                    if slug and slug not in skills_loaded:
                        skills_loaded.append(slug)
                    resp = {"ok": True, "result": {"loaded": slug}}
                elif method == "report_diagnostic":
                    diag_text = str(params.get("summary") or "").strip()
                    if diag_text:
                        diagnostics.append(diag_text)
                    resp = {"ok": True, "result": {"recorded": True}}
                elif method == "research_scout":
                    question = str(params.get("question") or "").strip()
                    if not question:
                        raise ValueError("research_scout requires a non-empty question.")
                    raw_paths = params.get("paths")
                    focus_paths = (
                        [str(p).strip() for p in raw_paths if str(p).strip()]
                        if isinstance(raw_paths, list)
                        else []
                    )
                    scout_root = worktree_dir if worktree_dir is not None else cwd
                    scout_text, scout_cost = await self._run_research_scout(
                        root_dir=scout_root,
                        session_dir=session_dir,
                        question=question,
                        paths=focus_paths,
                    )
                    if scout_cost > 0:
                        completed_cost += scout_cost
                        total_cost = completed_cost + streaming_cost
                        if (
                            initial_state.spent_today_usd + total_cost
                            > initial_state.daily_budget_usd
                        ):
                            budget_exceeded = True
                            proc_ref = self._active_procs.get(run_key)
                            if proc_ref and proc_ref.returncode is None:
                                proc_ref.terminate()
                    receipt = validate_evidence_receipt(scout_root, scout_text)
                    resp = {"ok": True, "result": receipt}
                elif method == "propose_skill_change":
                    if role != "improver":
                        raise ValueError("propose_skill_change is only permitted for the improver role.")
                    if modified_skills:
                        raise ValueError("Updates are scoped to one skill change per reflection run.")
                    raw_ids = params.get("evidence_attempt_ids") or []
                    proposal = SkillProposal(
                        action=params["action"],
                        slug=str(params["slug"]),
                        lesson=str(params["lesson"]),
                        description=str(params.get("description") or ""),
                        body_markdown=str(params.get("body_markdown") or ""),
                        evidence_attempt_ids=tuple(int(x) for x in raw_ids),
                        expected_sha256=(
                            str(params["expected_sha256"])
                            if params.get("expected_sha256") is not None
                            else None
                        ),
                        retire_slug=(
                            str(params["retire_slug"])
                            if params.get("retire_slug") is not None
                            else None
                        ),
                        retire_reason=(
                            str(params["retire_reason"])
                            if params.get("retire_reason") is not None
                            else None
                        ),
                    )
                    skill_row = apply_skill_proposal(
                        self.db,
                        self.cfg.skills_dir,
                        proposal,
                        max_personal_skills=self.cfg.max_personal_skills,
                    )
                    modified_skills.append(skill_row.slug)
                    resp = {
                        "ok": True,
                        "result": {
                            "slug": skill_row.slug,
                            "version": skill_row.version,
                            "status": skill_row.status,
                            "sha256": skill_row.sha256,
                        },
                    }
                elif method == "defer_for_approval":
                    summary_spoken = str(params["summary_spoken"])
                    details = str(params.get("details") or "")
                    sess_id = str(params.get("session_id") or "") or None
                    approved: bool | None = None
                    if approval_cb is not None and self.db.get_state().mode == "ASSIST":
                        approved = await approval_cb(summary_spoken, details, sess_id)
                    if approved is None:
                        deferred_flag = True
                        ob = self.db.queue_outbox(
                            kind="approval_request",
                            task_id=task.id if task else None,
                            resume_session_id=sess_id,
                            summary_spoken=summary_spoken,
                            details={"details": details},
                        )
                        resp = {
                            "ok": True,
                            "result": {"approved": False, "deferred": True, "outbox_id": ob.id},
                        }
                    else:
                        resp = {"ok": True, "result": {"approved": approved, "deferred": False}}
                else:
                    resp = {"ok": False, "error": f"Unknown RPC method: {method}"}
            except Exception as exc:
                resp = {"ok": False, "error": str(exc)}
            writer.write((json.dumps(resp) + "\n").encode("utf-8"))
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(handle_rpc, path=str(sock_path))
        os.chmod(sock_path, 0o600)

        if role == "builder":
            model = self.cfg.builder_model
            tools = BUILDER_TOOLS
        elif role == "improver":
            model = self.cfg.improver_model
            tools = IMPROVER_TOOLS
        else:
            model = self.cfg.planner_model
            tools = PLANNER_TOOLS

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
            "-e",
            str(self._board_ext_path),
            "--session-dir",
            str(session_dir),
            "--model",
            model,
            "-t",
            tools,
        ]
        if role in ("planner", "builder") and self.db.list_skills(status="active"):
            cmd.extend(["--skill", str(self.cfg.skills_dir)])
        if role == "improver":
            cmd.append("--offline")
        if resume_session_id:
            cmd.extend(["--session", resume_session_id])
        cmd.extend(["--", prompt])

        if role == "builder" and worktree_dir is not None:
            cmd = wrap_with_sandbox(
                cmd,
                worktree_dir=worktree_dir,
                session_dir=session_dir,
                notes_dir=self.cfg.notes_dir,
                home_dir=self.cfg.home_dir,
                enabled=self.cfg.sandbox_enabled,
            )

        env = self._build_sandbox_env(session_dir)
        env["SUPERBRAIN_RPC_SOCK"] = str(sock_path)
        env["SUPERBRAIN_SKILLS_DIR"] = str(self.cfg.skills_dir.resolve())
        if worktree_dir is not None:
            env["SUPERBRAIN_WORKTREE_DIR"] = str(worktree_dir.resolve())
        if task is not None:
            env["SUPERBRAIN_TASK_ID"] = task.id
            env["SUPERBRAIN_TASK_KIND"] = task.kind

        last_text = ""
        session_id = resume_session_id
        start_monotonic = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._active_procs[run_key] = proc
            assert proc.stdout is not None

            while True:
                remaining = self.cfg.max_run_seconds - (time.monotonic() - start_monotonic)
                if remaining <= 0:
                    timed_out = True
                    proc.terminate()
                    break
                try:
                    raw_line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=min(5.0, max(0.1, remaining))
                    )
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    continue
                if not raw_line:
                    break
                line_str = raw_line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                try:
                    event = json.loads(line_str)
                except json.JSONDecodeError:
                    last_text = line_str
                    continue
                if isinstance(event, dict):
                    if event.get("type") == "session" and isinstance(event.get("id"), str):
                        session_id = event["id"]
                    elif isinstance(event.get("session_id"), str):
                        session_id = event["session_id"]
                    elif isinstance(event.get("sessionId"), str):
                        session_id = event["sessionId"]
                    text = extract_assistant_text(event)
                    if text:
                        last_text = text
                    ev_cost = extract_event_cost(event)
                    if ev_cost > 0:
                        if event.get("type") == "message_update":
                            streaming_cost = max(streaming_cost, ev_cost)
                        else:
                            completed_cost += ev_cost
                            streaming_cost = 0.0
                        total_cost = completed_cost + streaming_cost
                        if initial_state.spent_today_usd + total_cost > initial_state.daily_budget_usd:
                            budget_exceeded = True
                            proc.terminate()
                            break

            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        finally:
            self._active_procs.pop(run_key, None)
            server.close()
            await server.wait_closed()
            sock_path.unlink(missing_ok=True)

        session_jsonl_path = self._find_session_jsonl(session_dir, session_id)
        diag_joined = "; ".join(diagnostics) if diagnostics else None

        if deferred_flag:
            return PiRunResult(
                outcome="deferred_approval",
                summary=last_text or "Deferred for owner approval.",
                cost_usd=total_cost,
                session_id=session_id,
                session_jsonl_path=session_jsonl_path,
                upserted_task_ids=upserted_ids,
                skills_used=skills_loaded,
                diagnostic=diag_joined,
                modified_skills=modified_skills,
            )
        if timed_out:
            return PiRunResult(
                outcome="timed_out",
                summary=f"Worker exceeded max_run_seconds ({self.cfg.max_run_seconds}s).",
                cost_usd=total_cost,
                session_id=session_id,
                session_jsonl_path=session_jsonl_path,
                upserted_task_ids=upserted_ids,
                skills_used=skills_loaded,
                diagnostic=diag_joined,
                modified_skills=modified_skills,
            )
        if budget_exceeded:
            return PiRunResult(
                outcome="errored",
                summary=f"Run stopped: daily budget (${initial_state.daily_budget_usd:.2f}) exceeded.",
                cost_usd=total_cost,
                session_id=session_id,
                session_jsonl_path=session_jsonl_path,
                upserted_task_ids=upserted_ids,
                skills_used=skills_loaded,
                diagnostic=diag_joined,
                modified_skills=modified_skills,
            )
        if proc.returncode != 0:
            return PiRunResult(
                outcome="errored",
                summary=stderr_text or last_text or f"pi exited with code {proc.returncode}",
                cost_usd=total_cost,
                session_id=session_id,
                session_jsonl_path=session_jsonl_path,
                upserted_task_ids=upserted_ids,
                skills_used=skills_loaded,
                diagnostic=diag_joined,
                modified_skills=modified_skills,
            )
        return PiRunResult(
            outcome="pushed",
            summary=last_text or "Completed turn.",
            cost_usd=total_cost,
            session_id=session_id,
            session_jsonl_path=session_jsonl_path,
            upserted_task_ids=upserted_ids,
            skills_used=skills_loaded,
            diagnostic=diag_joined,
            modified_skills=modified_skills,
        )

    async def _run_research_scout(
        self,
        *,
        root_dir: Path,
        session_dir: Path,
        question: str,
        paths: list[str],
    ) -> tuple[str, float]:
        """
        Spawns an isolated read-only `pi` scout subprocess to answer `question` with exact quotes
        copied byte-for-byte from files under `root_dir`, returning `(raw_output, cost_usd)`.
        """
        focus_hint = f"\nFocus paths: {', '.join(paths)}" if paths else ""
        scout_prompt = (
            "You are a read-only Evidence-Preserving Research Scout.\n"
            f"Question: {question}{focus_hint}\n"
            "Inspect the codebase using read/grep/find/ls and return ONE JSON object only (no markdown fences):\n"
            '{"summary": "<1-2 sentence answer>", "uncertain": false, '
            '"evidence": [{"file": "<relative/path>", '
            '"quote": "<exact contiguous lines copied byte-for-byte from file>"}]}\n'
            "Every `quote` MUST appear byte-for-byte in `file`."
        )
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
            "--no-session",
            "--model",
            self.cfg.scout_model,
            "-t",
            SCOUT_TOOLS,
            "--",
            scout_prompt,
        ]
        env = self._build_sandbox_env(session_dir)
        env["SUPERBRAIN_RESEARCH_SCOUT"] = "1"
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(root_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        last_text = ""
        completed_cost = 0.0
        streaming_cost = 0.0
        try:
            out_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=min(120, self.cfg.max_run_seconds)
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return '{"summary": "Scout timed out", "uncertain": true, "evidence": []}', 0.0

        for raw_line in out_bytes.decode("utf-8", errors="replace").splitlines():
            line_str = raw_line.strip()
            if not line_str:
                continue
            try:
                ev = json.loads(line_str)
            except json.JSONDecodeError:
                last_text = line_str
                continue
            if isinstance(ev, dict):
                txt = extract_assistant_text(ev)
                if txt:
                    last_text = txt
                c = extract_event_cost(ev)
                if c > 0:
                    if ev.get("type") == "message_update":
                        streaming_cost = max(streaming_cost, c)
                    else:
                        completed_cost += c
                        streaming_cost = 0.0
        return last_text, completed_cost + streaming_cost

    @staticmethod
    def _find_session_jsonl(session_dir: Path, session_id: str | None) -> str | None:
        if not session_dir.is_dir():
            return None
        candidates = sorted(session_dir.glob("*.jsonl"))
        if not candidates:
            return None
        if session_id:
            for c in reversed(candidates):
                if session_id in c.name:
                    return str(c)
        return str(candidates[-1])

    @staticmethod
    def _build_sandbox_env(session_dir: Path) -> dict[str, str]:
        tmp_dir = session_dir / "tmp"
        cache_dir = session_dir / "cache"
        tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = os.environ.copy()
        env["TMPDIR"] = str(tmp_dir)
        env["XDG_CACHE_HOME"] = str(cache_dir)
        env["UV_CACHE_DIR"] = str(cache_dir / "uv")
        return env

    async def _run_verify(
        self, worktree_dir: Path, session_dir: Path, verify_cmd: str
    ) -> tuple[bool, str]:
        cmd = wrap_with_sandbox(
            ["/bin/sh", "-c", verify_cmd],
            worktree_dir=worktree_dir,
            session_dir=session_dir,
            notes_dir=self.cfg.notes_dir,
            home_dir=self.cfg.home_dir,
            enabled=self.cfg.sandbox_enabled,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(worktree_dir),
            env=self._build_sandbox_env(session_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self.cfg.verify_timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, f"verify_cmd timed out after {self.cfg.verify_timeout_seconds}s"
        output = out_bytes.decode("utf-8", errors="replace").strip()[-4000:]
        return proc.returncode == 0, output

    async def _run_held_out_baseline_tests(
        self, repo_dir: Path, worktree_dir: Path, session_dir: Path
    ) -> tuple[bool, str]:
        """
        Runs the pre-edit baseline test suite from `repo_dir/tests` ($D_out$) against the candidate
        worktree's `src/` so a self-upgrade cannot pass verification by weakening existing tests.
        """
        baseline_tests = repo_dir / "tests"
        if not baseline_tests.is_dir() or not any(baseline_tests.glob("test_*.py")):
            return True, ""

        cmd = wrap_with_sandbox(
            [sys.executable, "-m", "pytest", "-q", str(baseline_tests)],
            worktree_dir=worktree_dir,
            session_dir=session_dir,
            notes_dir=self.cfg.notes_dir,
            home_dir=self.cfg.home_dir,
            enabled=self.cfg.sandbox_enabled,
        )
        env = self._build_sandbox_env(session_dir)
        py_parts = [str(worktree_dir / "src"), str(worktree_dir)]
        if env.get("PYTHONPATH"):
            py_parts.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(py_parts)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(worktree_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self.cfg.verify_timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, f"Held-out baseline tests timed out after {self.cfg.verify_timeout_seconds}s"
        output = out_bytes.decode("utf-8", errors="replace").strip()[-4000:]
        return proc.returncode == 0, output

    async def _worktree_diff_stat(self, worktree_dir: Path, base_sha: str) -> str:
        if not worktree_dir.exists():
            return ""
        await self._git_out(worktree_dir, "add", "-A", check=False)
        return (
            await self._git_out(
                worktree_dir, "diff", "--cached", "--stat", base_sha, check=False
            )
        ).strip()

    def _finalize_attempt_artifacts(self, task_id: str, *, diff_stat: str = "") -> None:
        attempts = self.db.list_attempts(task_id)
        if not attempts:
            return
        latest = attempts[-1]
        write_attempt_trace(self.cfg.traces_dir, latest, diff_stat=diff_stat)
        if latest.outcome in ("failed_verify", "timed_out", "errored"):
            excerpt = (latest.verify_output or latest.summary or "").strip()[-500:]
            touched = diff_stat or "(no file changes)"
            entry = (
                f"\n### Negative Result — Attempt #{latest.id} ({latest.outcome})\n"
                f"- Summary: {latest.summary}\n"
                f"- Touched: {touched}\n"
                f"- Failure: {excerpt}\n"
            )
            note_path = self.cfg.notes_dir / f"{task_id}.md"
            with note_path.open("a", encoding="utf-8") as f:
                f.write(entry)
            os.chmod(note_path, 0o600)

    async def _check_post_upgrade_health(self, *, succeeded: bool) -> None:
        rollback_sha = self.db.record_post_upgrade_outcome(succeeded=succeeded)
        if not rollback_sha:
            return
        repo_dir = (self.cfg.projects_dir / "superbrain").resolve()
        if (repo_dir / ".git").exists():
            dirty = (await self._git_out(repo_dir, "status", "--porcelain", check=False)).strip()
            if dirty:
                await self._git_out(
                    repo_dir,
                    "stash",
                    "push",
                    "-u",
                    "-m",
                    "superbrain-pre-runtime-rollback",
                    check=False,
                )
            await self._git_out(repo_dir, "reset", "--hard", rollback_sha, check=False)
        self.db.queue_outbox(
            kind="report",
            summary_spoken=(
                f"Automatically rolled back Superbrain to {rollback_sha[:7]} after "
                "2 consecutive post-upgrade task failures."
            ),
            details={"rollback_sha": rollback_sha},
        )
        self.restart_pending = True

    async def _find_deleted_tests(self, worktree_dir: Path, base_sha: str) -> list[str]:
        out = await self._git_out(
            worktree_dir,
            "diff",
            "--diff-filter=D",
            "--name-only",
            base_sha,
            "--",
            "tests",
            "test",
            check=False,
        )
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def _find_modified_protected_files(
        self, worktree_dir: Path, base_sha: str
    ) -> list[str]:
        await self._git_out(worktree_dir, "add", "-A", check=False)
        out = await self._git_out(
            worktree_dir, "diff", "--cached", "--name-only", base_sha, check=False
        )
        changed = {line.strip() for line in out.splitlines() if line.strip()}
        protected = set(changed & PROTECTED_SELF_PATHS)
        existing_test_mods = await self._git_out(
            worktree_dir,
            "diff",
            "--cached",
            "--diff-filter=DMR",
            "--name-only",
            base_sha,
            "--",
            "tests",
            "test",
            check=False,
        )
        for line in existing_test_mods.splitlines():
            if line.strip():
                protected.add(line.strip())
        return sorted(protected)

    async def _apply_self_upgrade(
        self, repo_dir: Path, verified_sha: str, task_id: str
    ) -> None:
        old_head = (await self._git_out(repo_dir, "rev-parse", "HEAD")).strip()
        self.db.set_last_good_sha(old_head)
        dirty = (await self._git_out(repo_dir, "status", "--porcelain", check=False)).strip()
        if dirty:
            await self._git_out(
                repo_dir,
                "stash",
                "push",
                "-u",
                "-m",
                f"superbrain-pre-upgrade-{task_id}",
                check=False,
            )
        await self._git_out(repo_dir, "reset", "--hard", verified_sha)
        remotes = await self._git_out(repo_dir, "remote", check=False)
        if "origin" in remotes.splitlines():
            await self._git_out(repo_dir, "push", "origin", "HEAD:master", check=False)
        self.restart_pending = True

    async def _git_fetch(self, repo_dir: Path) -> None:
        remotes = await self._git_out(repo_dir, "remote")
        if "origin" in remotes.splitlines():
            await self._git_out(repo_dir, "fetch", "origin", "--prune", check=False)

    async def _prepare_worktree(
        self, repo_dir: Path, worktree_dir: Path, branch_name: str
    ) -> str:
        if worktree_dir.exists():
            await self._cleanup_worktree(repo_dir, worktree_dir)
        has_origin_master = (
            await self._git_rc(repo_dir, "rev-parse", "--verify", "origin/master") == 0
        )
        base_ref = "origin/master" if has_origin_master else "HEAD"
        await self._git_out(
            repo_dir, "worktree", "add", "-B", branch_name, str(worktree_dir), base_ref
        )
        os.chmod(worktree_dir, 0o700)
        return (await self._git_out(worktree_dir, "rev-parse", "HEAD")).strip()

    async def _commit_and_push(
        self, worktree_dir: Path, branch_name: str, task: TaskRow
    ) -> str:
        await self._git_out(worktree_dir, "add", "-A")
        if await self._git_rc(worktree_dir, "diff", "--cached", "--quiet") != 0:
            await self._git_out(
                worktree_dir,
                "commit",
                "-m",
                f"feat(brain): resolve {task.id} - {task.title}",
            )
        sha = (await self._git_out(worktree_dir, "rev-parse", "HEAD")).strip()
        remotes = await self._git_out(worktree_dir, "remote")
        if "origin" in remotes.splitlines():
            await self._git_out(
                worktree_dir, "push", "-u", "origin", branch_name, "--force-with-lease"
            )
        return sha

    async def _cleanup_worktree(self, repo_dir: Path, worktree_dir: Path) -> None:
        if worktree_dir.exists():
            await self._git_out(
                repo_dir, "worktree", "remove", "--force", str(worktree_dir), check=False
            )
            if worktree_dir.exists():
                shutil.rmtree(worktree_dir, ignore_errors=True)

    @staticmethod
    async def _git_rc(cwd: Path, *args: str) -> int:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(cwd),
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait()

    @staticmethod
    async def _git_out(cwd: Path, *args: str, check: bool = True) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(cwd),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed in {cwd}: {err.decode('utf-8', errors='replace').strip()}"
            )
        return out.decode("utf-8", errors="replace")
