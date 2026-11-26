from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from loguru import logger

from brain import memory, schedule
from brain.config import SELF_REPOS, Settings, settings
from brain.db import (
    AttemptOutcome,
    BrainDB,
    TaskRow,
    TimerRow,
    TraceRunRow,
    WorkerRole,
    check_metric_target,
    has_metric_comparator,
)
from brain.evals import harvest_eval_cases
from brain.pareto import Candidate, parse_metrics, select_winner
from brain.pi_stream import PiStreamState
from brain.sandbox import strip_secrets, wrap_with_sandbox
from brain.skills import (
    SkillProposal,
    apply_skill_proposal,
    inspect_attempt_trajectory,
    mine_failure_patterns,
    record_skill_outcome,
    render_failure_patterns,
    sync_skills_on_disk,
    validate_evidence_receipt,
    write_attempt_trace,
)
from brain.telemetry import (
    EVENT_ATTEMPT_FINISHED,
    EVENT_BOARD_RPC,
    EVENT_PI_PROGRESS,
    EVENT_RECONCILER_PHASE,
    TelemetryHub,
    preview,
)
from brain.traces import HARNESS_SURFACES, classify_surface, distill_experience
from brain.upgrade import GitState, classify_git_state
from brain.weights import laya_intent_ready
from jarvis_core.home import JARVIS_HOME

ApprovalCallback = Callable[[str, str, str | None], Awaitable[bool | None]]

PLANNER_TOOLS = (
    "read,grep,find,ls,research_scout,upsert_task,report_diagnostic,defer_for_approval,"
    "schedule_followup"
)
BUILDER_TOOLS = (
    "read,edit,write,edit_and_run,bash,grep,find,ls,research_scout,"
    "record_note,report_diagnostic,defer_for_approval,schedule_followup"
)
IMPROVER_TOOLS = (
    "read,grep,find,ls,propose_skill_change,propose_memory_update,propose_harness_task,"
    "propose_weight_update"
)
SCOUT_TOOLS = "read,grep,find,ls"
_METRIC_LINE_RE = re.compile(r"\bMETRIC\s*[:=]\s*(-?\d+(?:\.\d+)?)(?![\d.])", re.IGNORECASE)
_METRIC_CMD_RE = re.compile(r"\bMETRIC\b", re.IGNORECASE)
_WORD_RE = re.compile(r"\w+")
PROTECTED_SELF_PATHS = {
    # the workspace and its locks: a change here alters everyone's toolchain and dependencies
    "pyproject.toml",
    "uv.lock",
    "Cargo.toml",
    "Cargo.lock",
    # what runs the workers, authenticates callers, and lets code write anywhere
    "apps/brain/src/brain/config.py",
    "apps/brain/src/brain/reconciler.py",
    "apps/brain/src/brain/sandbox.py",
    "apps/brain/src/brain/server.py",
    "apps/brain/src/brain/upgrade.py",
    # the tracer and its store: a worker that can edit these can fake its own evidence
    "apps/brain/src/brain/traces.py",
    "apps/brain/src/brain/evals.py",
    "apps/brain/src/brain/defaults/pi/board.ts",
    # shared state home, `.env` loading, and device credential store
    "packages/jarvis-core/src/jarvis_core/devices.py",
    "packages/jarvis-core/src/jarvis_core/home.py",
    "packages/jarvis-core/src/jarvis_core/settings.py",
    # the voice harness's own gatekeeping, guest guardrails, relay, and entry point
    "apps/voice/src/jarvis_voice/agent_tools.py",
    "apps/voice/src/jarvis_voice/config.py",
    "apps/voice/src/jarvis_voice/remote_audio.py",
    "apps/voice/src/jarvis_voice/voice_gates.py",
    "src/jarvis/cli.py",
    "src/jarvis/supervisor.py",
    # the benchmarks that decide whether a change is safe to keep
    "scripts/bench_intent.py",
    "scripts/bench_routing.py",
    "scripts/bench_brain_route.py",
    "scripts/bench_voice_prompts.py",
}
# A self-upgrade touching these runs the matching benchmark in `scripts/` before it can land.
VOICE_INTENT_PATHS = {
    "apps/voice/src/jarvis_voice/executive_filter.py",
    "apps/voice/src/jarvis_voice/languages.json",
}
VOICE_ROUTING_PATHS = {
    "apps/voice/src/jarvis_voice/prompts.py",
    "apps/voice/src/jarvis_voice/agent_tools.py",
    "apps/voice/src/jarvis_voice/orchestrator.py",
}
# The brain's router: without a number here, a self-upgrade can degrade daytime routing unnoticed.
BRAIN_ROUTE_PATHS = {"apps/brain/src/brain/orchestrator.py"}
# What Jarvis says after hearing the user: the cleaner, a retell and the "still working" lines.
VOICE_PROMPT_PATHS = {
    "apps/voice/src/jarvis_voice/cleanup.py",
    "apps/voice/src/jarvis_voice/prompts.py",
    "apps/voice/src/jarvis_voice/state.py",
    "apps/voice/src/jarvis_voice/system_prompts.py",
    "apps/voice/src/jarvis_voice/waiting_lines.py",
    "SYSTEM.md",
}
# suite -> (benchmark script, the paths that make it relevant); its `held_out` file is the sealed half.
EVAL_GATES: dict[str, tuple[str, set[str]]] = {
    "brain_route": ("scripts/bench_brain_route.py", BRAIN_ROUTE_PATHS),
    "voice_prompts": ("scripts/bench_voice_prompts.py", VOICE_PROMPT_PATHS),
}
HARNESS_PREDICTION_RUNS = 10  # runs on the target surface before a fix's prediction is checked
# The Improver's own probation: new evidence needed before its scorecard is allowed to judge the change.
PROBATION_MIN_TASKS = 10
PROBATION_MIN_TURNS = 30
IMPROVER_PATHS = {"apps/brain/src/brain/skills.py", "apps/brain/src/brain/reconciler.py"}
WEIGHT_TARGETS = ("whisper-owner-lora", "laya-intent", "router-distill")
WEIGHT_GATES = {
    "whisper-owner-lora": "uv run python scripts/voice/evaluate.py --split test",
    "laya-intent": "uv run python scripts/bench_intent.py 3",
    "router-distill": "uv run python scripts/bench_brain_route.py 3",
}
# Hypothesis novelty: embeddings separate mechanisms that share words; word overlap is the offline fallback.
OLLAMA_EMBED_URL = "http://127.0.0.1:11434/api/embed"
SPEC_COSINE_DUPLICATE = 0.88
SPEC_JACCARD_DUPLICATE = 0.8


def harness_spec_hash(spec: str) -> str:
    """Identity of a proposal's idea, indifferent to punctuation: the duplicate check's first key."""
    normalized = " ".join(_WORD_RE.findall(spec.lower()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def spec_similarity(left: str, right: str) -> float:
    """Jaccard overlap of two specs' words: 1.0 means a reworded copy of the same hypothesis."""
    left_words, right_words = set(_WORD_RE.findall(left.lower())), set(_WORD_RE.findall(right.lower()))
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / len(left_words | right_words)


async def embedding_cosine(left: str, right: str, *, client: httpx.AsyncClient | None = None) -> float:
    """bge-m3 cosine between two specs on the Ollama app already in RAM; raises when it cannot answer."""
    owned = client is None
    http = client or httpx.AsyncClient(timeout=1.5)
    try:
        resp = await http.post(
            OLLAMA_EMBED_URL,
            json={"model": "bge-m3", "input": [left, right], "keep_alive": -1},
        )
        resp.raise_for_status()
        first, second = resp.json()["embeddings"]
    finally:
        if owned:
            await http.aclose()
    dot = sum(a * b for a, b in zip(first, second))
    norms = sum(a * a for a in first) ** 0.5 * sum(b * b for b in second) ** 0.5
    return dot / norms if norms else 0.0


_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


async def is_duplicate_spec(left: str, right: str, *, client: httpx.AsyncClient | None = None) -> bool:
    """Two hypotheses that would test the same mechanism: cosine when the embedder answers, words otherwise.

    A differing numeric constant is a different experiment, not a copy: `Set CYCLES = 1400` and
    `Set CYCLES = 1100` are a legitimate sweep, and embeddings alone would call them identical.
    """
    if _NUMBER_RE.sub("#", left.lower()) == _NUMBER_RE.sub("#", right.lower()) and (
        _NUMBER_RE.findall(left) != _NUMBER_RE.findall(right)
    ):
        return False
    try:
        return await embedding_cosine(left, right, client=client) >= SPEC_COSINE_DUPLICATE
    except Exception:  # noqa: BLE001 - Ollama down or model unpulled: the word overlap still holds the line
        return spec_similarity(left, right) >= SPEC_JACCARD_DUPLICATE


def duplicate_harness_proposal(
    db: BrainDB, surface: str, spec: str
) -> Any | None:
    """The earlier proposal this spec repeats: a rejected or failed idea may not come back unchanged."""
    spec_hash = harness_spec_hash(spec)
    for previous in db.list_harness_proposals(surface=surface, since_days=30, limit=20):
        if previous.outcome == "proposed":
            continue  # still open: the dedupe key already stops a second row for that surface
        if previous.spec_hash == spec_hash or spec_similarity(spec, previous.spec) >= 0.8:
            return previous
    return None


def extract_metric_value(output: str | None) -> float | None:
    """Extracts the last `METRIC: <float>` or `METRIC=<float>` reported by `verify_cmd`."""
    if not output:
        return None
    matches = _METRIC_LINE_RE.findall(output)
    return float(matches[-1]) if matches else None


def _terminate_proc_group(
    proc: asyncio.subprocess.Process, sig: signal.Signals = signal.SIGTERM
) -> None:
    """Signals the entire subprocess group (`start_new_session=True`) so child runners cannot linger."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return
    except OSError:
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()


def _clean_untracked_tool_caches(worktree_dir: Path) -> None:
    for cache_name in (".pytest_cache", ".mypy_cache", ".ruff_cache"):
        cache_path = worktree_dir / cache_name
        if cache_path.is_dir() and not cache_path.is_symlink():
            shutil.rmtree(cache_path, ignore_errors=True)


def _is_test_path(path: str) -> bool:
    """True for a file in a `tests`/`test` folder anywhere in the tree: `tests/`, `apps/brain/tests/`."""
    return any(part in ("test", "tests") for part in Path(path).parts[:-1])


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
    modified_memory: list[str] | None = None


class Reconciler:
    """Deterministic goal and problem reconciler driving ephemeral `pi 1.5.0` workers."""

    def __init__(
        self,
        db: BrainDB,
        cfg: Settings = settings,
        on_restart_requested: Callable[[], None] | None = None,
        hub: TelemetryHub | None = None,
    ) -> None:
        self.db = db
        self.cfg = cfg
        self.on_restart_requested = on_restart_requested
        self.hub = hub if hub is not None else TelemetryHub()
        self.restart_pending: bool = False
        self._last_phase: str | None = None
        self.cfg.worktrees_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cfg.sessions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cfg.notes_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cfg.skills_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cfg.traces_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        memory.ensure_layout(self.cfg.home_dir)
        sync_skills_on_disk(self.db, self.cfg.skills_dir)
        self._board_ext_path = self._install_board_extension()
        self._loop_task: asyncio.Task[None] | None = None
        self._active_procs: dict[str, asyncio.subprocess.Process] = {}
        self._wake = asyncio.Event()
        self._timer_tasks: set[asyncio.Task[None]] = set()
        self._check_runner: Any | None = None
        self._last_reflection_date: str | None = None
        self.db.recover_firing_timers(max_age_seconds=max(1800, self.cfg.max_run_seconds * 2))

    def _repo_dir(self, repo: str) -> Path:
        """The checkout a task works on: this repository for self tasks, else `projects_dir/<repo>`."""
        allowed = self.db.validate_repo(repo)  # refuses anything outside ALLOWED_REPOS
        return self.cfg.self_repo_dir if allowed in SELF_REPOS else (self.cfg.projects_dir / allowed).resolve()

    def _install_board_extension(self) -> Path:
        ext_dir = self.cfg.home_dir / "ext"
        ext_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = ext_dir / "board.ts"
        source_text = (
            files("brain")
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
        for task in list(self._timer_tasks):
            task.cancel()
        if self._timer_tasks:
            await asyncio.gather(*list(self._timer_tasks), return_exceptions=True)
        self._timer_tasks.clear()
        if self._check_runner is not None:
            await self._check_runner.stop()
        for proc in list(self._active_procs.values()):
            _terminate_proc_group(proc, signal.SIGTERM)

    def abort_run(self, run_key: str) -> bool:
        aborted = False
        for key, proc in list(self._active_procs.items()):
            if (key == run_key or key.startswith(f"{run_key}:scout:")) and proc.returncode is None:
                _terminate_proc_group(proc, signal.SIGTERM)
                aborted = True
        return aborted

    def _set_phase(self, phase: str, *, force: bool = False, **extra: Any) -> None:
        """Publishes a reconciler phase transition (de-duplicated unless `force=True`)."""
        key = "|".join(
            [
                phase,
                str(extra.get("task_id") or ""),
                str(extra.get("role") or ""),
                str(extra.get("run_key") or ""),
            ]
        )
        if key == self._last_phase and not force:
            return
        self._last_phase = key
        self.hub.emit(EVENT_RECONCILER_PHASE, phase=phase, **extra)

    def _emit_attempt(self, task_id: str, role: str, outcome: str, **extra: Any) -> None:
        self.hub.emit(
            EVENT_ATTEMPT_FINISHED,
            task_id=task_id,
            role=role,
            outcome=outcome,
            **extra,
        )

    async def _run_loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[Reconciler] tick error: {exc!r}")
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.cfg.reconcile_interval_seconds
                )
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def wake(self) -> None:
        """A new task, timer or approval landed: claim it now instead of waiting for the next poll."""
        self._wake.set()

    async def tick(self) -> TaskRow | None:
        """Executes one deterministic reconciler cycle for the active mode (ASSIST or AUTONOMOUS)."""
        self.db.reap_stale_leases()
        self.db.recover_firing_timers(max_age_seconds=max(1800, self.cfg.max_run_seconds * 2))
        await self._fire_timers()
        await self._check_harness_predictions()
        await self._check_improver_probation()
        state = self.db.get_state()
        if state.spent_today_usd >= state.daily_budget_usd:
            self._set_phase("budget_paused", spent_usd=state.spent_today_usd)
            logger.warning("[Reconciler] Daily budget reached; pausing task claiming.")
            return None

        task = self.db.claim_next_task(
            lease_seconds=self.cfg.lease_seconds, mode=state.mode
        )
        if task is None:
            self._set_phase("idle", mode=state.mode)
            if state.mode == "AUTONOMOUS":
                unreflected = self.db.list_unreflected_attempts()
                should_reflect = len(unreflected) >= 2 or any(
                    a.outcome in ("failed_verify", "errored", "timed_out") or bool(a.diagnostic)
                    for a in unreflected
                )
                if should_reflect:
                    await self.run_skill_improver()
            elif self._nightly_reflection_due():
                # ASSIST mode used to teach the board nothing: the digest is read at night, edits stay drafts.
                self._last_reflection_date = datetime.now().astimezone().strftime("%Y-%m-%d")
                logger.info("[Reconciler] nightly reflection from the durable trace digest")
                await self.run_skill_improver()
            return None

        self._set_phase(
            "claimed",
            task_id=task.id,
            role="builder" if task.verify_cmd else "planner",
            mode=task.mode,
        )
        finished = await self.execute_claimed_task(task)
        self._sync_harness_outcome(finished)
        if self.restart_pending and self.on_restart_requested is not None:
            self.on_restart_requested()
        return finished

    async def _fire_timers(self) -> None:
        """Act on due timers: create a board task, or start a short check whose answer goes to the outbox."""
        for timer in self.db.fire_due_timers():
            if timer.action == "task":
                try:
                    repo = timer.repo or "jarvis"
                    self.db.upsert_task(
                        kind="goal",
                        repo=repo,
                        title=timer.title,
                        spec=timer.prompt_spec,
                        metric_target="The verify_cmd passes",
                        verify_cmd=timer.verify_cmd,
                        mode=timer.mode,
                        runner="pi",
                        dedupe_key=f"timer:{timer.id}:{timer.next_run_at}",
                        ready=True,
                    )
                    self.db.finish_timer(timer.id)
                except Exception as exc:
                    logger.error(f"[Reconciler] timer {timer.id} could not queue a task: {exc!r}")
                    self.db.queue_outbox(
                        kind="report",
                        task_id=timer.task_id,
                        summary_spoken=f"Scheduled task {timer.title!r} could not be queued: {exc}",
                        details={"timer_id": timer.id, "reason": "timer_error"},
                    )
                    self.db.finish_timer(timer.id)
                continue
            # A check is a short worker turn: run it beside the loop so a user task never waits behind it.
            task = asyncio.create_task(self._timer_check_job(timer), name=f"timer-{timer.id}")
            self._timer_tasks.add(task)
            task.add_done_callback(self._timer_tasks.discard)

    async def _timer_check_job(self, timer: TimerRow) -> None:
        try:
            await self._run_timer_check(timer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[Reconciler] timer {timer.id} failed: {exc!r}")
            self.db.queue_outbox(
                kind="report",
                task_id=timer.task_id,
                summary_spoken=f"Scheduled check {timer.title!r} failed: {exc}",
                details={"timer_id": timer.id, "reason": "timer_error"},
            )
        finally:
            self.db.finish_timer(timer.id)

    async def wait_for_timer_checks(self) -> None:
        """Tests and shutdown: let every started check finish (or be cancelled)."""
        while self._timer_tasks:
            await asyncio.gather(*list(self._timer_tasks), return_exceptions=True)

    async def _run_timer_check(self, timer: TimerRow) -> None:
        """A read-only, short `pi` turn for a due check; the summary is spoken through the outbox."""
        from brain.runners.base import RunRequest
        from brain.runners.ephemeral_pi import EphemeralPiRunner

        _, zone = schedule.resolve_timezone(timer.timezone)
        local_now = datetime.now(zone).strftime("%A %Y-%m-%d %H:%M %Z")
        repo_dir = self._repo_dir(timer.repo) if timer.repo else self.cfg.home_dir
        cwd = repo_dir if repo_dir.is_dir() else self.cfg.home_dir
        notes = memory.repos_dir(self.cfg.home_dir) / f"{memory.repo_slug(timer.repo)}.md" if timer.repo else None
        prompt = (
            "You are Brain's scheduled check, answering the owner aloud.\n"
            f"Current local time: {local_now}. Never call today yesterday.\n"
            f"Check: {timer.title}\n"
            f"Instructions: {timer.prompt_spec}\n"
            f"Known facts about this source or repository:\n"
            f"{notes.read_text(encoding='utf-8') if notes and notes.is_file() else '(none)'}\n"
            f"Bookmarks (read the source from its saved cursor, never a fixed time window):\n"
            f"{memory.render_bookmarks(self.cfg.home_dir)}\n\n"
            f"Recently reported to the owner:\n{memory.recent_reported(self.cfg.home_dir)}\n\n"
            "Use read/grep/find/ls and bash (curl, gh) when needed. If a source cannot be read, say it "
            "plainly; never report a failed read as 'nothing new'. Before repeating an item that is still "
            "open, re-check its live state and say 'still waiting, day N'; drop an item that was resolved. "
            "Reply in one or two short sentences that can be spoken aloud, with no preamble and no file "
            "paths.\n"
            "After a successful read, end with one line per source, exactly "
            "`BOOKMARK <source>=<newest cursor>` (for example `BOOKMARK gh-prs:jarvis=2026-11-22T08:00:00Z`). "
            "That line is machinery, not part of the spoken answer; omit it for a source you could not read."
        )
        if self._check_runner is None:
            self._check_runner = EphemeralPiRunner(self.cfg, self.db)
        runner = self._check_runner

        async def _emit(_event: dict[str, Any]) -> None:
            return None

        result = await runner.run(
            RunRequest(
                run_id=f"timer-{timer.id}",
                prompt=prompt,
                cwd=cwd,
                timeout_seconds=float(self.cfg.check_timeout_seconds),
                max_cost_usd=timer.max_cost_usd or self.cfg.max_ephemeral_cost_usd,
                append_system_paths=tuple(memory.addendum_paths(self.cfg.home_dir)),
            ),
            _emit,
        )
        spoken_lines: list[str] = []
        for line in result.summary.splitlines():
            bookmark = re.match(r"^\s*BOOKMARK\s+(\S+?)\s*=\s*(.+?)\s*$", line, re.IGNORECASE)
            if bookmark:
                try:
                    memory.write_bookmark(self.cfg.home_dir, bookmark.group(1), bookmark.group(2))
                except ValueError:
                    pass
                continue
            spoken_lines.append(line)
        spoken = " ".join(part.strip() for part in spoken_lines if part.strip()).strip()
        if result.outcome != "pushed" or not spoken:
            spoken = f"Scheduled check {timer.title!r} could not finish: {result.summary}"
        self.db.queue_outbox(
            kind="report",
            task_id=timer.task_id,
            summary_spoken=spoken[:600],
            details={
                "timer_id": timer.id,
                "outcome": result.outcome,
                "cost_usd": result.cost_usd,
            },
        )

    async def execute_claimed_task(
        self,
        task: TaskRow,
        *,
        run_key: str | None = None,
        resume_session_id: str | None = None,
        approval_cb: ApprovalCallback | None = None,
    ) -> TaskRow:
        notify_owner = not (run_key or "").startswith(("run-", "ask-"))

        def done(**kwargs: Any) -> TaskRow:
            # A voice-initiated run already tells the owner what happened; only background work needs the outbox.
            return self.db.finish_attempt(notify_owner=notify_owner, **kwargs)

        role: WorkerRole = "builder" if task.verify_cmd else "planner"
        self._set_phase("prepare", task_id=task.id, role=role)
        repo_dir = self._repo_dir(task.repo)
        if not (repo_dir / ".git").exists():
            return done(
                task_id=task.id,
                role=role,
                summary=f"Repository {task.repo!r} not found at {repo_dir}",
                outcome="errored",
            )

        approved_ob = self.db.get_unconsumed_approved_outbox(task.id)
        approved_details: dict[str, Any] = {}
        if approved_ob is not None:
            try:
                approved_details = json.loads(approved_ob.details_json or "{}")
            except Exception:
                approved_details = {}
            self.db.mark_outbox_consumed(approved_ob.id)

        effective_resume_id = resume_session_id or (
            approved_ob.resume_session_id if approved_ob is not None else None
        )

        # Fast-path for owner-approved protected self-upgrades: apply the exact pinned `approved_sha`
        # verified on the prior attempt instead of re-running Builder with guardrails disabled.
        pinned_sha = str(approved_details.get("approved_sha") or "").strip()
        if role == "builder" and task.repo in SELF_REPOS and pinned_sha:
            await self._git_fetch(repo_dir)
            if await self._git_rc(repo_dir, "cat-file", "-e", f"{pinned_sha}^{{commit}}") == 0:
                applied = await self._apply_self_upgrade(repo_dir, pinned_sha, task.id)
                finished_task = done(
                    task_id=task.id,
                    role="builder",
                    summary=(
                        "Applied owner-approved protected self-upgrade."
                        if applied
                        else "Refused owner-approved self-upgrade: checkout diverged from origin/master."
                    ),
                    outcome="pushed" if applied else "errored",
                    git_branch=f"brain/{task.id}",
                    git_sha=pinned_sha,
                )
                self._finalize_attempt_artifacts(task.id)
                return finished_task

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
                finished_task = done(
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
                finished_task = done(
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
            # Self-upgrades must build on top of what GitHub master actually has. Only keep local
            # HEAD when it carries unpublished commits that origin/master does not contain; if the
            # two histories diverged, refuse rather than reset the checkout backwards.
            prefer_local_head = False
            if task.repo in SELF_REPOS:
                base_state = await self._git_state_vs_origin_master(repo_dir)
                if base_state is GitState.DIVERGED:
                    await self._report_self_upgrade_divergence(
                        repo_dir, task.id, verified_sha="", phase="prepare"
                    )
                    finished_task = done(
                        task_id=task.id,
                        role="builder",
                        summary=(
                            "Refused self-upgrade: local HEAD and origin/master have diverged; "
                            "resolve the checkout before retrying."
                        ),
                        outcome="errored",
                    )
                    self._finalize_attempt_artifacts(task.id)
                    return finished_task
                prefer_local_head = base_state is GitState.LOCAL_AHEAD
            base_sha = await self._prepare_worktree(
                repo_dir,
                worktree_dir,
                branch_name,
                prefer_local_head=prefer_local_head,
            )
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
                finished_task = done(
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
                    finished_task = done(
                        task_id=task.id,
                        role="builder",
                        summary=pi_res.summary,
                        outcome="failed_verify",
                        cost_usd=pi_res.cost_usd,
                        pi_session_id=pi_res.session_id,
                        session_jsonl_path=pi_res.session_jsonl_path,
                        git_branch=branch_name,
                        verify_output=f"Blocked: problem task modified or deleted test files: {', '.join(deleted_tests)}",
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
            if task.repo in SELF_REPOS and self.cfg.self_upgrade_verify_cmd.strip():
                effective_verify_cmd = (
                    f"({task.verify_cmd}) && ({self.cfg.self_upgrade_verify_cmd})"
                )
            self._set_phase("verifying", task_id=task.id, role="builder")
            verified, verify_output = await self._run_verify(
                worktree_dir, session_dir, effective_verify_cmd
            )
            metric_val = extract_metric_value(verify_output)
            requires_metric_line = bool(_METRIC_CMD_RE.search(effective_verify_cmd))
            if not requires_metric_line and task.parent_id:
                parent_row = self.db.get_task(task.parent_id)
                requires_metric_line = parent_row.child_strategy == "any" and has_metric_comparator(
                    parent_row.metric_target or task.metric_target
                )
            if verified and requires_metric_line and metric_val is None:
                verified = False
                verify_output = (
                    f"{verify_output}\n"
                    "Expected METRIC: <number> in verify_cmd output, but none was found."
                ).strip()
            elif (
                verified
                and metric_val is not None
                and not check_metric_target(task.metric_target, metric_val)
            ):
                verified = False
                verify_output = (
                    f"{verify_output}\n"
                    f"Metric {metric_val} did not satisfy metric_target {task.metric_target!r}"
                ).strip()
            if verified and task.repo in SELF_REPOS:
                changed = await self._changed_files(worktree_dir, base_sha)
                verified, bench_out = await self._run_voice_benchmarks(
                    repo_dir, worktree_dir, changed, session_dir
                )
                if not verified:
                    verify_output = (
                        "Voice benchmark regressed (details sealed to prevent overfitting)."
                    )
                    self.db.queue_outbox(
                        kind="report",
                        task_id=task.id,
                        summary_spoken=f"Task {task.id} failed the voice benchmarks.",
                        details={"benchmark_output": bench_out},
                    )
            if verified and task.repo in SELF_REPOS:
                verified, eval_out = await self._run_eval_splits(
                    repo_dir, worktree_dir, task, changed, session_dir
                )
                if not verified:
                    verify_output = "Held-out eval cases regressed (details sealed to prevent overfitting)."
                    self.db.queue_outbox(
                        kind="report",
                        task_id=task.id,
                        summary_spoken=f"Task {task.id} failed the sealed eval split.",
                        details={"eval_output": eval_out},
                    )
            if verified and task.repo in SELF_REPOS:
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
                finished_task = done(
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

            self._set_phase("pushing", task_id=task.id, role="builder")
            git_sha = await self._commit_and_push(worktree_dir, branch_name, task)
            if task.repo in SELF_REPOS:
                touched_protected = await self._find_modified_protected_files(
                    worktree_dir, base_sha
                )
                if touched_protected:
                    prompt_spoken = (
                        f"Approve Brain self-upgrade modifying protected files "
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
                                details={
                                    "files": touched_protected,
                                    "approved_sha": git_sha,
                                    "summary": pi_res.summary,
                                },
                            )
                        finished_task = done(
                            task_id=task.id,
                            role="builder",
                            summary=f"Awaiting owner approval for protected files: {', '.join(touched_protected)}",
                            outcome="deferred_approval",
                            cost_usd=pi_res.cost_usd,
                            pi_session_id=pi_res.session_id,
                            session_jsonl_path=pi_res.session_jsonl_path,
                            git_branch=branch_name,
                            git_sha=git_sha,
                            skills_used=pi_res.skills_used,
                            diagnostic=pi_res.diagnostic,
                        )
                        self._finalize_attempt_artifacts(task.id, diff_stat=diff_stat)
                        return finished_task

            just_upgraded_self = False
            if task.repo in SELF_REPOS:
                if not await self._apply_self_upgrade(repo_dir, git_sha, task.id):
                    finished_task = done(
                        task_id=task.id,
                        role="builder",
                        summary=(
                            "Refused self-upgrade: checkout diverged from or is not based on "
                            "origin/master; it was not reset backwards."
                        ),
                        outcome="errored",
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
                        self.db, self.cfg.skills_dir, pi_res.skills_used or [], succeeded=False
                    )
                    return finished_task
                just_upgraded_self = True
            finished_task = done(
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
            finished_task = done(
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
            f"- {t.id} [{t.kind}/{t.mode}/{t.status}] ({t.repo}): {t.title} (verify={t.verify_cmd!r})"
            for t in self.db.list_tasks()[:20]
        )
        prompt = (
            f"You are Brain Planner running in {self.db.get_state().mode} mode.\n"
            f"Allowed repositories: {allowed}\n"
            f"Current tasks on board:\n{tasks_summary or '(none)'}\n\n"
            f"Owner request: {user_input}\n"
            "Use `upsert_task` to create or update tasks when appropriate. "
            "The background reconciler runs continuously: in ASSIST mode it resolves ASSIST tasks, "
            "and in AUTONOMOUS mode it resolves AUTONOMOUS tasks. "
            "When the owner (Sang) gives a new task or request:\n"
            "- If you can clearly see it is related to the current conversation topic or existing tasks "
            "currently being worked on (or the owner explicitly asks to resolve it now / in ASSIST mode), "
            "call `upsert_task` with `mode='ASSIST'` and `ready=true`.\n"
            "- If the owner explicitly asks to resolve it later in AUTONOMOUS / overnight mode, "
            "call `upsert_task` with `mode='AUTONOMOUS'` and `ready=true`.\n"
            "- If you are not sure whether it is related to the current topic/tasks or when the owner wants "
            "the issue resolved, ask the owner when you should resolve the issue before marking it ready.\n"
            "If the owner complains about a Brain/Jarvis bug or feature, or asks to upgrade this repository "
            "('jarvis', 'the brain', 'your code', 'your dashboard', 'brain'), call `upsert_task` with "
            "`repo='jarvis'`, `kind='problem'` (for bugs/complaints) or `kind='goal'` "
            "(for new features/upgrades), `mode='ASSIST'`, a deterministic `verify_cmd` "
            "(e.g. 'uv run pytest', or 'cargo test --manifest-path apps/dashboard/Cargo.toml' for dashboard work "
            "under `apps/dashboard/`), and `ready=true` so Brain immediately builds, verifies, and restarts itself. "
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

    def _nightly_reflection_due(self) -> bool:
        """Once a day, after the configured hour, with enough new experience to be worth a model call."""
        now = datetime.now().astimezone()
        today = now.strftime("%Y-%m-%d")
        if self._last_reflection_date == today or now.hour < self.cfg.nightly_reflect_hour:
            return False
        return self.db.count_unreflected_traces() >= 5

    def _sync_harness_outcome(self, finished: TaskRow | None) -> None:
        """`pushed` starts the prediction window; a failed verify closes the proposal as it stands.

        shortcut: only these two outcomes move a proposal here; a 2-crash rollback is the sha circuit breaker's
        job, and falsification (`_check_harness_predictions`) names the surface and the numbers.
        """
        if finished is None or not (finished.dedupe_key or "").startswith("harness:"):
            return
        attempts = self.db.list_attempts(finished.id)
        if not attempts:
            return
        outcome = {"pushed": "pushed", "failed_verify": "failed_verify"}.get(attempts[-1].outcome)
        if outcome is None:
            return
        self.db.set_harness_outcome(
            task_id=finished.id, outcome=outcome, applied_sha=finished.best_sha or None
        )

    async def _check_harness_predictions(self) -> None:
        """Self-Harness's falsifiable prediction: K runs after the fix, did the surface move as promised?

        A proposal that made its surface worse is marked `falsified` and offered for rollback; one that held
        is `verified`. Without this, a self-upgrade is never checked against the claim it shipped with.
        """
        for proposal in self.db.pushed_harness_proposals():
            if not proposal.applied_at:
                continue
            fails, total = self.db.surface_run_stats(
                proposal.surface, since=proposal.applied_at, limit=HARNESS_PREDICTION_RUNS
            )
            if total < HARNESS_PREDICTION_RUNS:
                continue
            rate = fails / total
            if fails >= 2 and rate > proposal.baseline_fail_rate:
                self.db.set_harness_outcome(proposal_id=proposal.id, outcome="falsified")
                self.db.queue_outbox(
                    kind="approval_request",
                    task_id=None,
                    summary_spoken=(
                        f"Roll back the {proposal.surface} fix? Its failure rate rose from "
                        f"{proposal.baseline_fail_rate:.0%} to {rate:.0%} over {total} runs."
                    ),
                    details={
                        "proposal_id": proposal.id,
                        "predicted_fix": proposal.predicted_fix,
                        "at_risk_regressions": proposal.at_risk_regressions,
                        "baseline_fail_rate": proposal.baseline_fail_rate,
                        "post_fail_rate": round(rate, 4),
                        "runs": total,
                        "rollback_sha": proposal.rollback_sha,
                    },
                )
            else:
                self.db.set_harness_outcome(proposal_id=proposal.id, outcome="verified")

    async def _check_improver_probation(self) -> None:
        """A self-upgrade that edits the Improver itself sits on a 7-day probation: its own numbers may not drop.

        Independent of the 2-crash breaker: this one looks at pass rate, interactive friction and cost per
        solved task, and offers the rollback sha rather than applying it on its own.
        """
        probation = self.db.improver_probation()
        if probation is None or not probation["until"]:
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if now >= probation["until"]:
            self.db.clear_improver_probation()
            return
        current = self.db.compute_meta_utility()
        if current["attempted_tasks"] < PROBATION_MIN_TASKS or current["turns"] < PROBATION_MIN_TURNS:
            return  # not enough new evidence to judge the change yet
        try:
            baseline = json.loads(probation["baseline_json"] or "{}")
        except ValueError:
            baseline = {}
        if not baseline:
            return
        dropped = (
            current["first_attempt_pass_rate"] < baseline.get("first_attempt_pass_rate", 0.0) - 0.10
            or current["friction_per_100_turns"] > baseline.get("friction_per_100_turns", 0.0) + 5.0
            or current["median_usd_per_solved_task"]
            > baseline.get("median_usd_per_solved_task", 0.0) * 1.35
        )
        if not dropped:
            return
        self.db.clear_improver_probation()
        self.db.queue_outbox(
            kind="approval_request",
            summary_spoken=(
                "Roll back the Improver change? Its own scorecard dropped during probation: "
                f"first-attempt pass rate {baseline.get('first_attempt_pass_rate', 0):.0%} to "
                f"{current['first_attempt_pass_rate']:.0%}."
            ),
            details={
                "reason": "improver_probation_regressed",
                "rollback_sha": probation["rollback_sha"],
                "baseline": baseline,
                "current": current,
            },
        )

    async def run_skill_improver(self, *, run_key: str = "improver") -> PiRunResult:
        """
        Runs the Perplexity-style 4-stage self-improvement reflection loop over unreflected attempts:
        1. Evidence: compact trajectory summaries + skill load / failure stats + diagnostics
        2. Reflection: offline `improver` agent proposes at most 1 skill create/update/retire
        3. Verification & 4. Activation: enforced deterministically in `propose_skill_change` RPC
        """
        unreflected = self.db.list_unreflected_attempts(limit=20)
        pending_traces = self.db.count_unreflected_traces()
        if not unreflected and pending_traces < 5:
            return PiRunResult(
                outcome="pushed",
                summary="No unreflected attempts or traces available for skill improvement.",
                cost_usd=0.0,
                modified_skills=[],
            )

        overview = distill_experience(self.db, self.cfg)
        # Real use becomes the next gate: the editable set grows, the sealed one grows where no worker reads.
        harvested = harvest_eval_cases(self.db, self.cfg)
        if any(harvested.values()):
            logger.info(f"[Reconciler] harvested eval cases: {harvested}")
        # Its own history: a rejected or falsified idea must not come back unchanged.
        proposal_history = "\n".join(
            f"- [{p.surface}] {p.outcome}: {p.title}"
            for p in self.db.list_harness_proposals(limit=10)
        ) or "(none yet)"

        session_dir = self.cfg.sessions_dir / "improver"
        session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._set_phase("skill_improver")
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
        memory_index = memory.memory_index(self.cfg.home_dir)
        memory_sha = memory.file_sha(self.cfg.memory_index_path) or ""
        repo_notes = [
            f"- {path.name} sha256={memory.file_sha(path) or ''} path={path}"
            for path in memory.repo_note_files(self.cfg.home_dir)
        ]
        patterns = mine_failure_patterns(
            self.cfg.traces_dir, JARVIS_HOME / "feedback" / "intent.jsonl"
        )
        prompt = (
            "You are Brain Skill Improver and memory curator. Use `read` and `grep` to inspect the trace "
            "files, skill files, standing instructions and memory index below before deciding anything.\n"
            "Start with the layered failure digest (clustered root causes, health per commit, failed-then-passed "
            f"pairs): {overview} . Drill into `analysis/surfaces/<surface>.md`, then the `trace-*.json` paths it "
            "cites, for the specific cases you act on. Do not paste raw traces into your reasoning.\n"
            "Rules:\n"
            "- Creating a new skill (`action='create'`) requires >= 2 independent attempt IDs showing "
            "the same recurring procedure or pitfall.\n"
            "- Prefer `action='append'` (with `expected_sha256`) to add an itemized rule bullet to an "
            "existing skill without rewriting or dropping its existing instructions; use `action='update'` "
            "only when refactoring conflicting lines.\n"
            f"- If active skills == {self.cfg.max_personal_skills}, creating a skill requires "
            "`retire_slug` and `retire_reason`.\n"
            "- Propose at most ONE skill change via `propose_skill_change`, or abstain.\n"
            "- If a fault recurs in the failure patterns below, you may instead queue at most ONE bounded "
            "harness fix via `propose_harness_task`: name the trace ids or corrections as evidence, a "
            "`verify_cmd` that fails today, the `target_surface` it belongs to, and the files you expect to "
            "change. A fix that needs a protected file is the owner's call, not yours. A proposal already "
            "rejected, failed or falsified may not be re-proposed unchanged.\n"
            "- A speech-check correction that keeps coming back is training data for the intent model, "
            "not a prompt edit; say so in the task spec instead of proposing a skill.\n"
            "- You may also propose at most ONE memory change via `propose_memory_update`:\n"
            "  - `name='MEMORY.md'` is the small index loaded into every run (150 lines max). Keep only "
            "standing facts, verified commands, and pointers; push repository detail into "
            "`repos/<slug>.md` (80 lines max).\n"
            "  - Before trusting an old note, check it against the trace and the live files; correct or "
            "drop a note that is wrong, and never turn a guess into a rule.\n"
            "  - Pass `expected_sha256` from the index below when updating an existing file.\n"
            "  - Never store secrets, raw diffs, or anything already visible from a single search.\n\n"
            f"Owner instructions: {self.cfg.instructions_path}\n"
            f"Memory index (sha256={memory_sha}):\n{memory_index}\n\n"
            f"Repo notes ({len(repo_notes)}):\n{chr(10).join(repo_notes) or '(none)'}\n\n"
            f"Active skills ({len(active_skills)}/{self.cfg.max_personal_skills}):\n"
            f"{chr(10).join(skill_index_lines) or '(none)'}\n\n"
            f"Unreflected attempt traces ({len(unreflected)}):\n"
            f"{chr(10).join(attempt_index_lines)}\n\n"
            f"Unreflected turn/run traces: {pending_traces} (in `trace_runs` and the digest above)\n\n"
            "Recurring failure patterns (grouped by harness surface; a narrow change should resolve one):\n"
            f"{render_failure_patterns(patterns)}\n\n"
            f"Your recent harness proposals and their outcomes:\n{proposal_history}"
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
            self.db.mark_traces_reflected(
                [row.trace_id for row in self.db.list_trace_runs(limit=200, reflected=0, exclude_boards=True)]
            )
        return res

    async def run_auto_research(self, task_id: str, *, run_key: str | None = None) -> PiRunResult:
        """
        Runs a SoL-Pi style Auto-Research Loop (Oracle Analysis + Breadth-First Hypothesis Fan-Out)
        for `task_id`: inspects past attempt trajectories and notes, optionally runs `research_scout`
        for byte-verified evidence, and fans out 2-3 competing child hypothesis tasks (`SG-<id>.1`,
        `SG-<id>.2`, ...) evaluated under the parent's verification and metric gates.
        """
        task = self.db.get_task(task_id)
        if task.status == "in_progress":
            raise ValueError(f"Cannot run auto-research while task {task.id!r} is in_progress.")
        repo_dir = self._repo_dir(task.repo)
        cwd = repo_dir if repo_dir.is_dir() else self.cfg.home_dir
        session_dir = self.cfg.sessions_dir / f"{task.id}-research"
        session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._set_phase("auto_research", task_id=task.id, role="planner")

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
            f"You are Brain Auto-Research Planner (SoL-Pi Oracle + Breadth-First Hypothesis Search).\n"
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
            "when optimizing a numeric metric), `metric_target`, and `ready=true`. Each child must pursue a "
            "different mechanism or file: a reworded sibling is rejected, and the parent's `verify_cmd` is "
            "inherited when a child leaves it out.\n"
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
            hypothesis_mode=True,
        )
        if res.cost_usd > 0:
            self.db.add_spend(res.cost_usd)
        child_ids = [uid for uid in (res.upserted_task_ids or []) if uid != task.id]
        if child_ids:
            latest = self.db.get_task(task.id)
            self.db.upsert_task(
                task_id=latest.id,
                parent_id=latest.parent_id,
                kind=latest.kind,
                repo=latest.repo,
                title=latest.title,
                spec=latest.spec,
                metric_target=latest.metric_target,
                verify_cmd=latest.verify_cmd,
                priority=latest.priority,
                child_strategy="any",
                ready=False,
            )
        return res

    def _build_planner_prompt(self, task: TaskRow) -> str:
        return (
            f"Decompose task {task.id} ({task.title}) in repo {task.repo!r} into 1-3 atomic child tasks "
            f"(or 2-3 competing auto-research hypothesis tasks when exploring an optimization or uncertain root cause) "
            f"using `upsert_task` with `parent_id={task.id!r}`, each with a deterministic `verify_cmd` "
            f"(which may print `METRIC: <float>` for quantitative goals), `metric_target`, and `ready=true`. "
            "Competing hypotheses must name a different mechanism or file each: a reworded copy of a "
            "sibling is rejected, and the parent's `verify_cmd` is inherited when a child leaves it out. "
            f"Use `research_scout` first when you need byte-verified codebase evidence. "
            f"Use `schedule_followup` only for a check that cannot run now (delay >= 5 minutes); "
            f"at most {self.cfg.max_agent_followups_per_task} follow-ups per task. "
            f"Current local time: {datetime.now().astimezone().strftime('%A %Y-%m-%d %H:%M %Z')}.\n"
            f"Spec: {task.spec}"
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
            f"Current local time: {datetime.now().astimezone().strftime('%A %Y-%m-%d %H:%M %Z')}.\n"
            f"Worktree: {worktree_dir}\n"
            f"Spec: {task.spec}\n"
            f"Metric target: {task.metric_target or '(pass verification)'}\n"
            f"Verification gate (`verify_cmd`): {task.verify_cmd}\n"
            f"Previous attempts:\n{history or '(none)'}\n"
            f"Progress & negative-result notes:\n{notes or '(none)'}\n\n"
            f"{anti_repeat}"
            "Prefer `research_scout` to delegate read-only reconnaissance with byte-verified quotes, "
            "and `edit_and_run` (Action Fusion) to fuse file edits with immediate test execution in one turn. "
            "Record verified facts as well as failures with `record_note`, and use `schedule_followup` "
            "only when a check truly cannot run in this turn. "
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
        hypothesis_mode: bool = False,
    ) -> PiRunResult:
        self._set_phase(
            "pi_running",
            task_id=task.id if task else None,
            role=role,
            run_key=run_key,
        )
        sock_dir = Path(tempfile.mkdtemp(prefix="sb-", dir="/tmp"))
        os.chmod(sock_dir, 0o700)
        sock_path = sock_dir / "rpc.sock"
        deferred_flag = False
        upserted_ids: list[str] = []
        skills_loaded: list[str] = []
        diagnostics: list[str] = []
        modified_skills: list[str] = []
        modified_memory: list[str] = []
        stream_state = PiStreamState(session_id=resume_session_id)
        extra_scout_cost = 0.0
        timed_out = False
        budget_exceeded = False
        cap_hit = False
        start_monotonic = time.monotonic()
        initial_state = self.db.get_state()
        per_run_cap = {
            "builder": self.cfg.max_builder_cost_usd,
            "planner": self.cfg.max_planner_cost_usd,
            "improver": self.cfg.max_improver_cost_usd,
        }.get(role, self.cfg.max_planner_cost_usd)

        async def handle_rpc(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal deferred_flag, extra_scout_cost, budget_exceeded, cap_hit
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
                    parent_id = params.get("parent_id") or (task.id if task else None)
                    parent_row = self.db.get_task(str(parent_id)) if parent_id else None
                    competing = hypothesis_mode or (
                        parent_row is not None and parent_row.child_strategy == "any"
                    )
                    if competing and parent_id:
                        # Competing hypotheses: no reworded copies, and one gate for all of them.
                        for sibling in self.db.list_tasks():
                            if sibling.parent_id != parent_id or sibling.id == params.get("id"):
                                continue
                            if await is_duplicate_spec(
                                str(params.get("spec") or ""), sibling.spec
                            ):
                                raise ValueError(
                                    f"Hypothesis duplicates sibling {sibling.id}; "
                                    "take a distinct mechanism, not a reworded one."
                                )
                        if (
                            parent_row is not None
                            and parent_row.verify_cmd
                            and not str(params.get("verify_cmd") or "").strip()
                        ):
                            params["verify_cmd"] = parent_row.verify_cmd
                    raw_mode = params.get("mode")
                    t = self.db.upsert_task(
                        task_id=params.get("id"),
                        parent_id=parent_id,
                        kind=params["kind"],
                        mode=str(raw_mode) if raw_mode is not None else (task.mode if task else None),
                        repo=params["repo"],
                        title=params["title"],
                        spec=params["spec"],
                        metric_target=params.get("metric_target"),
                        verify_cmd=params.get("verify_cmd"),
                        priority=int(params.get("priority", 50)),
                        child_strategy=params.get("child_strategy"),
                        ready=params.get("ready"),
                    )
                    upserted_ids.append(t.id)
                    resp: dict[str, Any] = {
                        "ok": True,
                        "result": {"id": t.id, "status": t.status, "mode": t.mode},
                    }
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
                    spent_so_far = (
                        initial_state.spent_today_usd + stream_state.total_cost + extra_scout_cost
                    )
                    budget_left = initial_state.daily_budget_usd - spent_so_far
                    if budget_left <= 0:
                        budget_exceeded = True
                        raise ValueError("Daily budget reached; research_scout skipped.")
                    raw_paths = params.get("paths")
                    focus_paths = (
                        [str(p).strip() for p in raw_paths if str(p).strip()]
                        if isinstance(raw_paths, list)
                        else []
                    )
                    scout_root = worktree_dir if worktree_dir is not None else cwd
                    remaining_secs = max(
                        1.0, self.cfg.max_run_seconds - (time.monotonic() - start_monotonic)
                    )
                    scout_text, scout_cost, scout_over_budget = await self._run_research_scout(
                        root_dir=scout_root,
                        session_dir=session_dir,
                        question=question,
                        paths=focus_paths,
                        run_key=f"{run_key}:scout:{uuid.uuid4().hex[:8]}",
                        timeout_seconds=min(120.0, remaining_secs),
                        budget_limit_usd=min(budget_left, self.cfg.max_scout_cost_usd),
                    )
                    extra_scout_cost += scout_cost
                    spent_now = (
                        initial_state.spent_today_usd
                        + stream_state.total_cost
                        + extra_scout_cost
                    )
                    daily_hit = spent_now > initial_state.daily_budget_usd
                    if daily_hit or scout_over_budget:
                        if scout_over_budget and not daily_hit:
                            cap_hit = True
                        else:
                            budget_exceeded = True
                        proc_ref = self._active_procs.get(run_key)
                        if proc_ref is not None:
                            _terminate_proc_group(proc_ref, signal.SIGTERM)
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
                elif method == "propose_memory_update":
                    if role != "improver":
                        raise ValueError("propose_memory_update is only permitted for the improver role.")
                    if modified_memory:
                        raise ValueError("Updates are scoped to one memory change per reflection run.")
                    name = str(params.get("name") or "").strip()
                    content = str(params.get("content") or "")
                    expected = (
                        str(params["expected_sha256"])
                        if params.get("expected_sha256") is not None
                        else None
                    )
                    written, new_sha = memory.write_memory_file(
                        self.cfg.home_dir, name, content, expected_sha256=expected
                    )
                    modified_memory.append(written)
                    resp = {
                        "ok": True,
                        "result": {"name": written, "sha256": new_sha},
                    }
                elif method == "propose_harness_task":
                    if role != "improver":
                        raise ValueError(
                            "propose_harness_task is only permitted for the improver role."
                        )
                    title = str(params.get("title") or "").strip()
                    spec = str(params.get("spec") or "").strip()
                    verify_cmd = str(params.get("verify_cmd") or "").strip()
                    evidence = str(params.get("evidence") or "").strip()
                    surface = str(params.get("target_surface") or "").strip()
                    predicted_fix = str(params.get("predicted_fix") or "").strip()
                    at_risk = str(params.get("at_risk_regressions") or "").strip()
                    raw_targets = params.get("target_files") or []
                    targets = (
                        [str(p).strip() for p in raw_targets if str(p).strip()]
                        if isinstance(raw_targets, list)
                        else []
                    )
                    if not title or not spec or not verify_cmd:
                        raise ValueError(
                            "A harness task needs a title, a spec and a reproduce-first verify_cmd."
                        )
                    if surface not in HARNESS_SURFACES:
                        raise ValueError(
                            f"target_surface must be one of: {', '.join(sorted(HARNESS_SURFACES))}."
                        )
                    if len(evidence) < 40:
                        raise ValueError(
                            "A harness task needs evidence: name the trace ids or corrections."
                        )
                    editable = set(HARNESS_SURFACES[surface].editable_files)
                    protected = sorted((set(targets) & PROTECTED_SELF_PATHS) - editable)
                    if protected:
                        raise ValueError(
                            f"Protected files belong to the owner: {', '.join(protected)}."
                        )
                    # A rejected or failed idea is never re-proposed unchanged: the loop remembers its own history.
                    spec_hash = harness_spec_hash(spec)
                    duplicate = duplicate_harness_proposal(self.db, surface, spec)
                    if duplicate is not None:
                        raise ValueError(
                            f"{surface} proposal #{duplicate.id} was already {duplicate.outcome}: "
                            f"{duplicate.title!r}. Take a different mechanism or leave it alone."
                        )
                    fails, runs = self.db.surface_run_stats(surface)
                    queued_task = self.db.upsert_task(
                        kind="problem",
                        repo="jarvis",
                        title=title[:120],
                        spec=(
                            f"{spec}\n\nEvidence:\n{evidence}\n\n"
                            f"Predicted fix: {predicted_fix or '(not stated)'}\n"
                            f"At risk: {at_risk or '(not stated)'}"
                        ),
                        verify_cmd=verify_cmd,
                        dedupe_key=f"harness:{surface}",
                        ready=self.cfg.autonomous_harness_upgrades,
                    )
                    recorded = self.db.record_harness_proposal(
                        surface=surface,
                        spec_hash=spec_hash,
                        title=title[:120],
                        spec=spec,
                        predicted_fix=predicted_fix,
                        at_risk_regressions=at_risk,
                        baseline_fail_rate=(fails / runs) if runs else 0.0,
                        baseline_runs=runs,
                        task_id=queued_task.id,
                    )
                    resp = {
                        "ok": True,
                        "result": {
                            "id": queued_task.id,
                            "status": queued_task.status,
                            "proposal_id": recorded.id,
                            "baseline_fail_rate": round(recorded.baseline_fail_rate, 4),
                            "baseline_runs": runs,
                        },
                    }
                elif method == "propose_weight_update":
                    if role != "improver":
                        raise ValueError(
                            "propose_weight_update is only permitted for the improver role."
                        )
                    target = str(params.get("target") or "").strip()
                    if target not in WEIGHT_TARGETS:
                        raise ValueError(f"target must be one of: {', '.join(WEIGHT_TARGETS)}")
                    reason = str(params.get("reason") or "").strip()
                    if len(reason) < 40:
                        raise ValueError("A weight update needs a reason: what the data shows.")
                    if target == "laya-intent":
                        cursor = int(memory.read_bookmarks(self.cfg.home_dir).get("laya_intent_cursor", "0") or 0)
                        ready_now, why = laya_intent_ready(JARVIS_HOME, bookmark_cursor=cursor)
                    else:
                        ready_now, why = True, "thresholds are checked by the training task itself"
                    queued_weight = self.db.upsert_task(
                        kind="problem",
                        repo="jarvis",
                        title=f"Retrain {target}",
                        spec=(
                            f"{reason}\n\nTarget: {target}\nGate: {WEIGHT_GATES[target]}\n"
                            f"Readiness: {why}\n"
                            "Train overnight, keep the best validation epoch, and promote only when the gate"
                            " improves; keep the previous checkpoint for a one-step rollback."
                        ),
                        verify_cmd=WEIGHT_GATES[target],
                        dedupe_key=f"weights:{target}",
                        ready=False,
                    )
                    resp = {
                        "ok": True,
                        "result": {
                            "id": queued_weight.id,
                            "status": queued_weight.status,
                            "ready": ready_now,
                            "gate": why,
                        },
                    }
                elif method == "schedule_followup":
                    if role not in ("planner", "builder"):
                        raise ValueError("schedule_followup is only permitted for planner or builder roles.")
                    when = str(params.get("delay_minutes") or "").strip()
                    if not when:
                        raise ValueError("schedule_followup needs delay_minutes")
                    try:
                        delay = int(float(when))
                    except ValueError as exc:
                        raise ValueError(f"Invalid delay_minutes: {when!r}") from exc
                    if delay < 5:
                        raise ValueError("a follow-up must wait at least 5 minutes")
                    if delay > 60 * 24 * 14:
                        raise ValueError("a follow-up may wait at most 14 days")
                    task_ref = task.id if task else None
                    if self.db.count_agent_followups(task_ref) >= self.cfg.max_agent_followups_per_task:
                        raise ValueError(
                            f"task {task_ref!r} already has "
                            f"{self.cfg.max_agent_followups_per_task} agent follow-ups"
                        )
                    reason = str(params.get("reason") or "").strip() or "Check the earlier work again."
                    run_at = datetime.now().astimezone() + timedelta(minutes=delay)
                    timer = self.db.add_timer(
                        kind="followup",
                        action="check",
                        title=f"Follow up on {task_ref or 'the earlier work'}",
                        prompt_spec=reason,
                        run_at=run_at,
                        repo=task.repo if task else None,
                        mode="ASSIST",
                        created_by="agent",
                        task_id=task_ref,
                        max_cost_usd=self.cfg.max_ephemeral_cost_usd,
                    )
                    resp = {
                        "ok": True,
                        "result": {"timer_id": timer.id, "next_run_at": timer.next_run_at},
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
            result_obj = resp.get("result")
            result: dict[str, Any] = result_obj if isinstance(result_obj, dict) else {}
            detail = ""
            if method == "research_scout":
                detail = (
                    f"{result.get('verified_count', 0)} verified / "
                    f"{result.get('rejected_count', 0)} rejected"
                )
            elif method == "upsert_task":
                detail = str(result.get("id") or "")
            elif method == "propose_skill_change":
                detail = f"{result.get('slug')} v{result.get('version')}"
            elif method == "propose_memory_update":
                detail = str(result.get("name") or "")
            elif method == "propose_harness_task":
                detail = str(result.get("id") or "")
            elif method == "propose_weight_update":
                detail = str(result.get("id") or "")
            elif method == "schedule_followup":
                detail = str(result.get("timer_id") or "")
            elif method == "defer_for_approval":
                detail = "deferred" if result.get("deferred") else str(result.get("approved"))
            elif method == "record_skill_load":
                detail = str(result.get("loaded") or "")
            elif method == "record_note":
                detail = str(result.get("saved") or "")
            self.hub.emit(
                EVENT_BOARD_RPC,
                run_key=run_key,
                task_id=task.id if task else None,
                role=role,
                method=str(method),
                ok=bool(resp.get("ok")),
                detail=detail,
                error=preview(str(resp.get("error"))) if resp.get("error") else None,
            )
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
            "-t",
            tools,
        ]
        if model.strip():
            cmd.extend(["--model", model.strip()])
        if role in ("planner", "builder") and self.db.list_skills(status="active"):
            cmd.extend(["--skill", str(self.cfg.skills_dir)])
        if role == "improver":
            cmd.append("--offline")
        if resume_session_id:
            cmd.extend(["--session", resume_session_id])
        if role in ("planner", "builder", "improver"):
            for path in memory.addendum_paths(self.cfg.home_dir):
                cmd.extend(["--append-system-prompt", str(path)])
        cmd.extend(["--", prompt])

        sandbox_target = worktree_dir if (role == "builder" and worktree_dir is not None) else session_dir
        cmd = wrap_with_sandbox(
            cmd,
            worktree_dir=sandbox_target,
            session_dir=session_dir,
            notes_dir=self.cfg.notes_dir,
            home_dir=self.cfg.home_dir,
            allow_read_traces=(role == "improver"),
            enabled=self.cfg.sandbox_enabled,
        )

        env = self._build_sandbox_env(session_dir)
        env["BRAIN_RPC_SOCK"] = str(sock_path)
        env["BRAIN_SKILLS_DIR"] = str(self.cfg.skills_dir.resolve())
        env["BRAIN_MEMORY_DIR"] = str(self.cfg.memory_dir.resolve())
        env["BRAIN_INSTRUCTIONS_PATH"] = str(self.cfg.instructions_path.resolve())
        if worktree_dir is not None:
            env["BRAIN_WORKTREE_DIR"] = str(worktree_dir.resolve())
        if task is not None:
            env["BRAIN_TASK_ID"] = task.id
            env["BRAIN_TASK_KIND"] = task.kind

        def over_budget() -> bool:
            nonlocal budget_exceeded, cap_hit
            spent = initial_state.spent_today_usd + stream_state.total_cost + extra_scout_cost
            if spent > initial_state.daily_budget_usd:
                budget_exceeded = True
                return True
            if per_run_cap > 0 and stream_state.total_cost + extra_scout_cost > per_run_cap:
                cap_hit = True
                return True
            return False

        returncode, stderr_text, timed_out, stream_over_budget = await self._stream_pi_proc(
            cmd=cmd,
            cwd=cwd,
            env=env,
            run_key=run_key,
            stream_state=stream_state,
            timeout_seconds=float(self.cfg.max_run_seconds),
            start_monotonic=start_monotonic,
            over_budget_fn=over_budget,
            cleanup_cb=self._make_rpc_cleanup(server, sock_path),
            progress_meta={
                "run_key": run_key,
                "role": role,
                "task_id": task.id if task else None,
            },
        )
        if not cap_hit:
            budget_exceeded = budget_exceeded or stream_over_budget
        self._set_phase("pi_finished", task_id=task.id if task else None, role=role)

        total_cost = stream_state.total_cost + extra_scout_cost
        last_text = stream_state.last_text
        session_id = stream_state.session_id
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
                modified_memory=modified_memory,
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
                modified_memory=modified_memory,
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
                modified_memory=modified_memory,
            )
        if cap_hit:
            return PiRunResult(
                outcome="errored",
                summary=f"Run stopped: per-run cost cap (${per_run_cap:.2f}) exceeded.",
                cost_usd=total_cost,
                session_id=session_id,
                session_jsonl_path=session_jsonl_path,
                upserted_task_ids=upserted_ids,
                skills_used=skills_loaded,
                diagnostic=diag_joined,
                modified_skills=modified_skills,
                modified_memory=modified_memory,
            )
        if returncode != 0:
            return PiRunResult(
                outcome="errored",
                summary=stderr_text or last_text or f"pi exited with code {returncode}",
                cost_usd=total_cost,
                session_id=session_id,
                session_jsonl_path=session_jsonl_path,
                upserted_task_ids=upserted_ids,
                skills_used=skills_loaded,
                diagnostic=diag_joined,
                modified_skills=modified_skills,
                modified_memory=modified_memory,
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
            modified_memory=modified_memory,
        )

    @staticmethod
    def _make_rpc_cleanup(
        server: asyncio.AbstractServer, sock_path: Path
    ) -> Callable[[], Awaitable[None]]:
        async def _cleanup() -> None:
            server.close()
            await server.wait_closed()
            sock_path.unlink(missing_ok=True)
            shutil.rmtree(sock_path.parent, ignore_errors=True)

        return _cleanup

    async def _stream_pi_proc(
        self,
        *,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        run_key: str,
        stream_state: PiStreamState,
        timeout_seconds: float,
        start_monotonic: float,
        over_budget_fn: Callable[[], bool],
        cleanup_cb: Callable[[], Awaitable[None]] | None = None,
        progress_meta: dict[str, Any] | None = None,
    ) -> tuple[int, str, bool, bool]:
        """Spawns a `pi` process in its own process group and streams JSON lines with live timeout/budget kill."""
        timed_out = False
        over_budget = False
        last_progress_emit = 0.0
        meta = progress_meta or {}
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._active_procs[run_key] = proc
        assert proc.stdout is not None
        try:
            while True:
                remaining = timeout_seconds - (time.monotonic() - start_monotonic)
                if remaining <= 0:
                    timed_out = True
                    _terminate_proc_group(proc, signal.SIGTERM)
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
                stream_state.feed_line(raw_line.decode("utf-8", errors="replace").strip())
                now_monotonic = time.monotonic()
                if now_monotonic - last_progress_emit >= 0.5:
                    last_progress_emit = now_monotonic
                    self.hub.emit(
                        EVENT_PI_PROGRESS,
                        run_key=meta.get("run_key", run_key),
                        role=meta.get("role"),
                        task_id=meta.get("task_id"),
                        session_id=stream_state.session_id,
                        cost_usd=round(stream_state.total_cost, 4),
                        elapsed_s=round(now_monotonic - start_monotonic, 1),
                        last_text_preview=preview(stream_state.last_text, 200),
                    )
                if over_budget_fn():
                    over_budget = True
                    _terminate_proc_group(proc, signal.SIGTERM)
                    break

            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                _terminate_proc_group(proc, signal.SIGKILL)
                await proc.wait()

            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            return proc.returncode or 0, stderr_text, timed_out, over_budget
        finally:
            self._active_procs.pop(run_key, None)
            if cleanup_cb is not None:
                await cleanup_cb()

    async def _run_research_scout(
        self,
        *,
        root_dir: Path,
        session_dir: Path,
        question: str,
        paths: list[str],
        run_key: str,
        timeout_seconds: float,
        budget_limit_usd: float,
    ) -> tuple[str, float, bool]:
        """
        Spawns a Seatbelt-sandboxed read-only `pi` scout subprocess to answer `question` with exact
        quotes copied byte-for-byte from files under `root_dir`, streaming output against both
        `timeout_seconds` and `budget_limit_usd`.
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
        scout_session_dir = session_dir / "scout"
        scout_session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw_cmd = [
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
            "-t",
            SCOUT_TOOLS,
        ]
        if self.cfg.scout_model.strip():
            raw_cmd.extend(["--model", self.cfg.scout_model.strip()])
        raw_cmd.extend(["--", scout_prompt])
        cmd = wrap_with_sandbox(
            raw_cmd,
            worktree_dir=root_dir,
            session_dir=scout_session_dir,
            notes_dir=self.cfg.notes_dir,
            home_dir=self.cfg.home_dir,
            enabled=self.cfg.sandbox_enabled,
        )
        env = self._build_sandbox_env(scout_session_dir)
        env["BRAIN_RESEARCH_SCOUT"] = "1"
        scout_state = PiStreamState()
        _, _, timed_out, over_budget = await self._stream_pi_proc(
            cmd=cmd,
            cwd=root_dir,
            env=env,
            run_key=run_key,
            stream_state=scout_state,
            timeout_seconds=timeout_seconds,
            start_monotonic=time.monotonic(),
            over_budget_fn=lambda: scout_state.total_cost > budget_limit_usd,
        )
        if timed_out:
            return (
                '{"summary": "Scout timed out", "uncertain": true, "evidence": []}',
                scout_state.total_cost,
                over_budget,
            )
        return scout_state.last_text, scout_state.total_cost, over_budget

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
        gh_dir = session_dir / "gh"
        tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        gh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = os.environ.copy()
        env["TMPDIR"] = str(tmp_dir)
        env["XDG_CACHE_HOME"] = str(cache_dir)
        env["UV_CACHE_DIR"] = str(cache_dir / "uv")
        env["GH_CONFIG_DIR"] = str(gh_dir)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "credential.helper"
        env["GIT_CONFIG_VALUE_0"] = ""
        return strip_secrets(env)

    async def _run_sandboxed_cmd(
        self,
        raw_cmd: list[str],
        *,
        worktree_dir: Path,
        session_dir: Path,
        label: str,
        extra_pythonpath: list[str] | None = None,
        timeout_seconds: float | None = None,
        extra_env: dict[str, str] | None = None,
        allow_read_held_out: bool = False,
    ) -> tuple[bool, str]:
        cmd = wrap_with_sandbox(
            raw_cmd,
            worktree_dir=worktree_dir,
            session_dir=session_dir,
            notes_dir=self.cfg.notes_dir,
            home_dir=self.cfg.home_dir,
            allow_read_held_out=allow_read_held_out,
            enabled=self.cfg.sandbox_enabled,
        )
        env = self._build_sandbox_env(session_dir)
        env.update(extra_env or {})
        if extra_pythonpath:
            py_parts = list(extra_pythonpath)
            if env.get("PYTHONPATH"):
                py_parts.append(env["PYTHONPATH"])
            env["PYTHONPATH"] = os.pathsep.join(py_parts)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(worktree_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        budget = timeout_seconds or self.cfg.verify_timeout_seconds
        try:
            out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=budget)
        except asyncio.TimeoutError:
            _terminate_proc_group(proc, signal.SIGKILL)
            await proc.wait()
            return False, f"{label} timed out after {budget}s"
        output = out_bytes.decode("utf-8", errors="replace").strip()[-4000:]
        return proc.returncode == 0, output

    async def _run_verify(
        self, worktree_dir: Path, session_dir: Path, verify_cmd: str
    ) -> tuple[bool, str]:
        return await self._run_sandboxed_cmd(
            ["/bin/sh", "-c", verify_cmd],
            worktree_dir=worktree_dir,
            session_dir=session_dir,
            label="verify_cmd",
        )

    async def _changed_files(self, worktree_dir: Path, base_sha: str) -> set[str]:
        """Paths the candidate changed, relative to the checkout root."""
        await self._git_out(worktree_dir, "add", "-A", check=False)
        out = await self._git_out(
            worktree_dir, "diff", "--cached", "--name-only", base_sha, check=False
        )
        return {line.strip() for line in out.splitlines() if line.strip()}

    async def _run_voice_benchmarks(
        self, repo_dir: Path, worktree_dir: Path, changed: set[str], session_dir: Path
    ) -> tuple[bool, str]:
        """The mandatory benchmarks when a self-upgrade touches the speech check, the router or what Jarvis says.

        The script comes from the checkout, never from the candidate, so a worker cannot rewrite the gate
        it is measured by; `PYTHONPATH` still loads the candidate's modules.
        """
        gates: list[tuple[str, list[str]]] = []
        if changed & VOICE_INTENT_PATHS:
            gates.append(("scripts/bench_intent.py", ["1"]))
        if changed & VOICE_ROUTING_PATHS:
            gates.append(("scripts/bench_routing.py", ["1"]))
        if changed & VOICE_PROMPT_PATHS:
            gates.append(("scripts/bench_voice_prompts.py", ["--split", "held_in"]))
        if changed & BRAIN_ROUTE_PATHS:
            gates.append(("scripts/bench_brain_route.py", ["1", "--split", "held_in"]))
        if not gates:
            return True, ""
        if not self.cfg.openrouter_api_key:
            self.db.queue_outbox(
                kind="blocker",
                summary_spoken=(
                    "A voice or router change needs its benchmark, but no OPENROUTER_API_KEY is set, "
                    "so the gate was skipped."
                ),
                details={"changed": sorted(changed)},
            )
            return True, ""
        sources = [
            worktree_dir / "apps" / "voice" / "src",
            worktree_dir / "apps" / "brain" / "src",
            worktree_dir / "packages" / "jarvis-core" / "src",
        ]
        outputs: list[str] = []
        for script, extra_args in gates:
            ok, output = await self._run_sandboxed_cmd(
                [sys.executable, str(repo_dir / script), *extra_args],
                worktree_dir=worktree_dir,
                session_dir=session_dir,
                label=script,
                extra_pythonpath=[str(path) for path in sources if path.is_dir()],
                timeout_seconds=self.cfg.verify_timeout_seconds * 6,
                extra_env={
                    "OPENROUTER_API_KEY": self.cfg.openrouter_api_key,
                    "BRAIN_HOME": str(self.cfg.home_dir),
                },
            )
            outputs.append(f"{script}: {'pass' if ok else 'FAIL'}\n{output[-1500:]}")
            if not ok:
                return False, "\n\n".join(outputs)
        return True, "\n\n".join(outputs)

    async def _run_eval_splits(
        self,
        repo_dir: Path,
        worktree_dir: Path,
        task: TaskRow,
        changed: set[str],
        session_dir: Path,
    ) -> tuple[bool, str]:
        """The sealed half of Phase 2: no held-out case may regress, and a harness fix must fix a held-in one.

        Both halves run from `repo_dir` with the code under test on `PYTHONPATH`: the baseline is the current
        checkout, the candidate is this worktree. A held-out failure is reported sealed, like the baseline suite.
        """
        suites = [name for name, (_, paths) in EVAL_GATES.items() if changed & paths]
        if not suites or not self.cfg.openrouter_api_key:
            return True, ""
        outputs: list[str] = []
        for suite in suites:
            script, _ = EVAL_GATES[suite]
            if not (self.cfg.home_dir / "evals" / "held_out" / f"{suite}.jsonl").is_file():
                continue  # nothing harvested yet: Phase 2 has no sealed set to protect
            baseline, base_metrics = await self._run_eval_split(
                repo_dir, repo_dir, worktree_dir, script, suite, "held_out", session_dir
            )
            candidate, cand_metrics = await self._run_eval_split(
                repo_dir, worktree_dir, worktree_dir, script, suite, "held_out", session_dir
            )
            if not baseline or not candidate:
                return False, f"{suite}: held-out eval failed to produce results"
            regressed = sorted(key for key, ok in baseline.items() if ok and not candidate.get(key, False))
            if regressed:
                return False, f"{suite}: {len(regressed)} held-out case(s) regressed"
            if base_metrics and cand_metrics:
                winner = select_winner(
                    [
                        Candidate(
                            "candidate",
                            accuracy=cand_metrics.get("accuracy", cand_metrics.get("pass_rate", 0.0)),
                            p50_ms=cand_metrics.get("p50_ms", 0.0),
                            cost_usd=cand_metrics.get("cost_usd", 0.0),
                        )
                    ],
                    baseline=Candidate(
                        "baseline",
                        accuracy=base_metrics.get("accuracy", base_metrics.get("pass_rate", 0.0)),
                        p50_ms=base_metrics.get("p50_ms", 0.0),
                        cost_usd=base_metrics.get("cost_usd", 0.0),
                    ),
                )
                if winner is None:
                    # The plan's tournament gate: cheaper accuracy is not worth more voice latency.
                    return False, f"{suite}: the candidate did not earn the change on accuracy, latency and cost"
            is_harness_fix = (task.dedupe_key or "").startswith("harness:")
            if is_harness_fix:
                base_in, _ = await self._run_eval_split(
                    repo_dir, repo_dir, worktree_dir, script, suite, "held_in", session_dir
                )
                cand_in, _ = await self._run_eval_split(
                    repo_dir, worktree_dir, worktree_dir, script, suite, "held_in", session_dir
                )
                if not base_in or not cand_in:
                    return False, f"{suite}: held-in eval failed to produce results"
                fixed = [key for key, ok in base_in.items() if not ok and cand_in.get(key)]
                if not fixed:
                    return False, f"{suite}: a harness fix must make at least one held-in case pass"
            outputs.append(f"{suite}: {len(candidate)} held-out cases, 0 regressions")
        return True, "\n\n".join(outputs)

    async def _run_eval_split(
        self,
        script_root: Path,
        code_root: Path,
        worktree_dir: Path,
        script: str,
        suite: str,
        split: str,
        session_dir: Path,
    ) -> tuple[dict[str, bool], dict[str, float]]:
        """One benchmark run: case results and parsed `METRICS:` (empty cases fail closed in caller)."""
        dump = session_dir / f"eval-{suite}-{split}-{code_root.name}.json"
        dump.unlink(missing_ok=True)
        sources = [code_root / "src", code_root]
        sources += sorted((code_root / "apps").glob("*/src"))
        sources += sorted((code_root / "packages").glob("*/src"))
        _, output = await self._run_sandboxed_cmd(
            [sys.executable, str(script_root / script), "--split", split, "--dump-results", str(dump)],
            worktree_dir=worktree_dir,
            session_dir=session_dir,
            label=f"{script} {split}",
            extra_pythonpath=[str(path) for path in sources if path.is_dir()],
            timeout_seconds=self.cfg.verify_timeout_seconds * 8,
            extra_env={
                "OPENROUTER_API_KEY": self.cfg.openrouter_api_key,
                "BRAIN_HOME": str(self.cfg.home_dir),
            },
            allow_read_held_out=True,
        )
        try:
            raw = json.loads(dump.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}, parse_metrics(output)
        cases = raw.get("cases") if isinstance(raw, dict) else None
        if not isinstance(cases, dict):
            return {}, parse_metrics(output)
        passed = {str(key): bool(value) for key, value in cases.items()}
        return passed, parse_metrics(output)

    async def _run_held_out_baseline_tests(
        self, repo_dir: Path, worktree_dir: Path, session_dir: Path
    ) -> tuple[bool, str]:
        """
        Runs the pre-edit baseline suites (root `tests/`, every `apps/*/tests/`) from `repo_dir` against the
        candidate worktree's sources, so a self-upgrade cannot pass verification by weakening a test.
        """
        roots = [
            path
            for path in (repo_dir / "tests", *sorted((repo_dir / "apps").glob("*/tests")))
            if path.is_dir() and any(path.glob("test_*.py"))
        ]
        if not roots:
            return True, ""
        sources = [worktree_dir / "src", worktree_dir]
        sources += sorted((worktree_dir / "apps").glob("*/src"))
        sources += sorted((worktree_dir / "packages").glob("*/src"))
        return await self._run_sandboxed_cmd(
            [sys.executable, "-m", "pytest", "-q", *(str(root) for root in roots)],
            worktree_dir=worktree_dir,
            session_dir=session_dir,
            label="Held-out baseline tests",
            extra_pythonpath=[str(path) for path in sources if path.is_dir()],
        )

    async def _worktree_diff_stat(self, worktree_dir: Path, base_sha: str) -> str:
        if not worktree_dir.exists():
            return ""
        _clean_untracked_tool_caches(worktree_dir)
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
        trace_file = write_attempt_trace(self.cfg.traces_dir, latest, diff_stat=diff_stat)
        repeated: tuple[str, ...] = ()
        try:
            oracle = json.loads(trace_file.read_text(encoding="utf-8")).get("oracle") or {}
            repeated = tuple(str(t) for t in oracle.get("repeated_error_tools") or ())
        except (OSError, ValueError):
            repeated = ()
        surface, causal = classify_surface(
            latest.outcome, repeated_error_tools=repeated, used_skills=bool(latest.skills_used)
        )
        self.db.upsert_trace_run(
            TraceRunRow(
                trace_id=f"attempt-{latest.id}",
                turn_id="",
                source="board",
                surface=surface or "builder_harness",
                runner="pi",
                repo="",
                task_id=task_id,
                attempt_id=latest.id,
                outcome="ok" if latest.outcome in ("pushed", "decomposed", "deferred_approval") else latest.outcome,
                friction_kind=(
                    None
                    if latest.outcome in ("pushed", "decomposed", "deferred_approval")
                    else f"attempt_{latest.outcome}"
                ),
                causal_sig=causal,
                latency_ms=None,
                ttfb_ms=None,
                tokens_in=0,
                tokens_out=0,
                cost_usd=float(latest.cost_usd or 0.0),
                git_sha=str(latest.git_sha or ""),
                prompt_sha="",
                skills_used=tuple(latest.skills_used),
                user_input=str(latest.summary or "")[:600],
                output_summary=str(latest.summary or "")[:280],
                trace_path=str(trace_file.relative_to(self.cfg.traces_dir)),
                superseded_by=None,
                reflected=0,
            )
        )
        self._emit_attempt(
            task_id,
            role=latest.role,
            outcome=latest.outcome,
            attempt_id=latest.id,
            cost_usd=latest.cost_usd,
            metric_value=latest.metric_value,
            git_branch=latest.git_branch,
            summary=preview(latest.summary),
        )
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
        elif latest.outcome in ("pushed", "decomposed"):
            # A memory that records only failures throws away verified facts and working commands
            # (Anthropic's CL-Bench study measured a large loss from that rule). Keep both.
            excerpt = (latest.verify_output or "").strip()[-300:]
            metric = f", metric={latest.metric_value}" if latest.metric_value is not None else ""
            entry = (
                f"\n### Verified — Attempt #{latest.id} ({latest.outcome})\n"
                f"- Summary: {latest.summary}\n"
                f"- Branch: {latest.git_branch or '(none)'}{metric}\n"
                f"- Verified by: {excerpt or '(no verify output recorded)'}\n"
            )
            note_path = self.cfg.notes_dir / f"{task_id}.md"
            with note_path.open("a", encoding="utf-8") as f:
                f.write(entry)
            os.chmod(note_path, 0o600)

    async def _check_post_upgrade_health(self, *, succeeded: bool) -> None:
        rollback_sha = self.db.record_post_upgrade_outcome(succeeded=succeeded)
        if not rollback_sha:
            return
        repo_dir = self.cfg.self_repo_dir
        if (repo_dir / ".git").exists():
            dirty = (await self._git_out(repo_dir, "status", "--porcelain", check=False)).strip()
            if dirty:
                await self._git_out(
                    repo_dir,
                    "stash",
                    "push",
                    "-u",
                    "-m",
                    "brain-pre-runtime-rollback",
                    check=False,
                )
            await self._git_out(repo_dir, "reset", "--hard", rollback_sha, check=False)
            remotes = await self._git_out(repo_dir, "remote", check=False)
            if "origin" in remotes.splitlines():
                await self._git_out(
                    repo_dir, "push", "origin", "HEAD:master", "--force-with-lease", check=False
                )
        self.db.queue_outbox(
            kind="report",
            summary_spoken=(
                f"Automatically rolled back Brain to {rollback_sha[:7]} after "
                "2 consecutive post-upgrade task failures."
            ),
            details={"rollback_sha": rollback_sha},
        )
        self.restart_pending = True

    async def _find_deleted_tests(self, worktree_dir: Path, base_sha: str) -> list[str]:
        await self._git_out(worktree_dir, "add", "-A", check=False)
        out = await self._git_out(
            worktree_dir, "diff", "--cached", "--diff-filter=DMR", "--name-only", base_sha, check=False
        )
        return [
            line.strip()
            for line in out.splitlines()
            if line.strip() and _is_test_path(line.strip())
        ]

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
            check=False,
        )
        for line in existing_test_mods.splitlines():
            if line.strip() and _is_test_path(line.strip()):
                protected.add(line.strip())
        return sorted(protected)

    async def _git_ref_sha(self, repo_dir: Path, ref: str) -> str | None:
        if await self._git_rc(repo_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}") != 0:
            return None
        return (await self._git_out(repo_dir, "rev-parse", "--verify", f"{ref}^{{commit}}")).strip()

    async def _git_state_vs_origin_master(self, repo_dir: Path) -> GitState:
        local_sha = await self._git_ref_sha(repo_dir, "HEAD")
        origin_sha = await self._git_ref_sha(repo_dir, "origin/master")
        if origin_sha is None:
            return GitState.UP_TO_DATE
        merge_base: str | None = None
        if local_sha is not None:
            if await self._git_rc(repo_dir, "merge-base", "HEAD", "origin/master") == 0:
                merge_base = (
                    await self._git_out(repo_dir, "merge-base", "HEAD", "origin/master")
                ).strip()
        return classify_git_state(local_sha, origin_sha, merge_base)

    async def _report_self_upgrade_divergence(
        self, repo_dir: Path, task_id: str, *, verified_sha: str, phase: str
    ) -> None:
        local_sha = await self._git_ref_sha(repo_dir, "HEAD") or "unknown"
        origin_sha = await self._git_ref_sha(repo_dir, "origin/master") or "unknown"
        logger.error(
            f"[Reconciler] self-upgrade {task_id} ({phase}): local HEAD {local_sha[:7]} and "
            f"origin/master {origin_sha[:7]} diverged; refusing to reset the checkout backwards."
        )
        self.db.queue_outbox(
            kind="blocker",
            task_id=task_id,
            summary_spoken=(
                f"Brain self-upgrade {task_id} refused: local HEAD {local_sha[:7]} and "
                f"origin/master {origin_sha[:7]} have diverged. Resolve the checkout and retry."
            ),
            details={
                "reason": "diverged_from_origin_master",
                "phase": phase,
                "local_head": local_sha,
                "origin_master": origin_sha,
                "verified_sha": verified_sha,
                "remedy": (
                    f"git -C {repo_dir} fetch origin --prune && "
                    f"git -C {repo_dir} rebase origin/master  # or reset --hard after saving local commits"
                ),
            },
        )

    async def _report_self_upgrade_stale_base(
        self, repo_dir: Path, task_id: str, *, verified_sha: str, origin_sha: str
    ) -> None:
        logger.error(
            f"[Reconciler] self-upgrade {task_id}: candidate {verified_sha[:7]} is not a descendant "
            f"of origin/master {origin_sha[:7]}; refusing to rewind the checkout."
        )
        self.db.queue_outbox(
            kind="blocker",
            task_id=task_id,
            summary_spoken=(
                f"Brain self-upgrade {task_id} refused: origin/master moved to {origin_sha[:7]} "
                f"while the candidate was built. Rebase the task branch on master and retry."
            ),
            details={
                "reason": "origin_master_advanced_past_candidate",
                "verified_sha": verified_sha,
                "origin_master": origin_sha,
                "remedy": (
                    f"git -C {repo_dir} branch brain/{task_id} {verified_sha} && "
                    f"git -C {repo_dir} rebase origin/master"
                ),
            },
        )

    async def _report_self_upgrade_push_rejected(
        self, repo_dir: Path, task_id: str, *, local_sha: str, detail: str
    ) -> None:
        origin_sha = await self._git_ref_sha(repo_dir, "origin/master") or "unknown"
        logger.error(
            f"[Reconciler] self-upgrade {task_id}: git push origin HEAD:master was rejected "
            f"(local {local_sha[:7]}, origin/master {origin_sha[:7]}): {detail}"
        )
        self.db.queue_outbox(
            kind="report",
            task_id=task_id,
            summary_spoken=(
                f"Brain self-upgrade {task_id} applied locally but could not be pushed to "
                f"origin/master; master moved to {origin_sha[:7]}. Local checkout needs a manual merge."
            ),
            details={
                "reason": "push_rejected_non_fast_forward",
                "local_head": local_sha,
                "origin_master": origin_sha,
                "git_error": detail,
                "remedy": (
                    f"git -C {repo_dir} fetch origin --prune && "
                    f"git -C {repo_dir} rebase origin/master && "
                    f"git -C {repo_dir} push origin HEAD:master"
                ),
            },
        )

    async def _apply_self_upgrade(
        self, repo_dir: Path, verified_sha: str, task_id: str
    ) -> bool:
        """Applies a verified self-upgrade in place. Returns False when divergence forced a refusal."""
        old_head = (await self._git_out(repo_dir, "rev-parse", "HEAD")).strip()
        remotes = await self._git_out(repo_dir, "remote", check=False)
        has_origin = "origin" in remotes.splitlines()
        if has_origin:
            await self._git_fetch(repo_dir)
            if await self._git_state_vs_origin_master(repo_dir) is GitState.DIVERGED:
                await self._report_self_upgrade_divergence(
                    repo_dir, task_id, verified_sha=verified_sha, phase="apply"
                )
                return False
            origin_sha = await self._git_ref_sha(repo_dir, "origin/master")
            if (
                origin_sha is not None
                and origin_sha != verified_sha
                and await self._git_rc(
                    repo_dir, "merge-base", "--is-ancestor", origin_sha, verified_sha
                )
                != 0
            ):
                # The candidate is not a descendant of the current master (master moved while the
                # builder ran); applying it would rewind the checkout and the push would be rejected.
                await self._report_self_upgrade_stale_base(
                    repo_dir, task_id, verified_sha=verified_sha, origin_sha=origin_sha
                )
                return False
        self.db.set_last_good_sha(old_head)
        dirty = (await self._git_out(repo_dir, "status", "--porcelain", check=False)).strip()
        if dirty:
            await self._git_out(
                repo_dir,
                "stash",
                "push",
                "-u",
                "-m",
                f"brain-pre-upgrade-{task_id}",
                check=False,
            )
        await self._git_out(repo_dir, "reset", "--hard", verified_sha)
        new_head = (await self._git_out(repo_dir, "rev-parse", "HEAD")).strip()
        changed = set(
            (
                await self._git_out(
                    repo_dir, "diff", "--name-only", old_head, new_head, check=False
                )
            ).splitlines()
        )
        if changed & IMPROVER_PATHS:
            # A change to the loop's own brain gets a week of numbers before it is trusted.
            self.db.start_improver_probation(
                rollback_sha=old_head,
                baseline_json=json.dumps(self.db.compute_meta_utility()),
                until_iso=(datetime.now(timezone.utc) + timedelta(days=7)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            )
            logger.info("[Reconciler] Improver self-upgrade on 7-day probation")
        dep_diff = (
            await self._git_out(
                repo_dir,
                "diff",
                "--name-only",
                old_head,
                new_head,
                "--",
                "pyproject.toml",
                "uv.lock",
                check=False,
            )
        ).strip()
        uv_exe = shutil.which("uv")
        if dep_diff and uv_exe:
            sync_proc = await asyncio.create_subprocess_exec(
                uv_exe,
                "sync",
                cwd=str(repo_dir),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await sync_proc.wait()
        rust_changed = any(
            p in ("Cargo.toml", "Cargo.lock") or p.startswith("apps/dashboard/")
            for p in changed
        )
        cargo_exe = shutil.which("cargo")
        if rust_changed and cargo_exe:
            cargo_proc = await asyncio.create_subprocess_exec(
                cargo_exe,
                "build",
                "--release",
                "--manifest-path",
                str(repo_dir / "apps" / "dashboard" / "Cargo.toml"),
                cwd=str(repo_dir),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await cargo_proc.wait()
        if has_origin:
            # Never swallow this: a rejected push means origin/master moved ahead of the checkout.
            push_rc, push_detail = await self._git_capture(repo_dir, "push", "origin", "HEAD:master")
            if push_rc != 0:
                await self._report_self_upgrade_push_rejected(
                    repo_dir, task_id, local_sha=new_head, detail=push_detail
                )
        self.db.set_harness_outcome(
            task_id=task_id, outcome="pushed", applied_sha=new_head, rollback_sha=old_head
        )
        self.restart_pending = True
        return True

    async def apply_rollback(self, rollback_sha: str, *, proposal_id: int | None = None) -> bool:
        """Reset the self checkout to a recorded rollback SHA, push if origin exists, and mark restart pending."""
        repo_dir = self.cfg.self_repo_dir
        if not (repo_dir / ".git").exists():
            return False
        if await self._git_rc(repo_dir, "cat-file", "-e", f"{rollback_sha}^{{commit}}") != 0:
            logger.error(f"[Reconciler] rollback SHA {rollback_sha!r} not found in {repo_dir}")
            return False
        dirty = (await self._git_out(repo_dir, "status", "--porcelain", check=False)).strip()
        if dirty:
            await self._git_out(
                repo_dir,
                "stash",
                "push",
                "-u",
                "-m",
                "brain-pre-approved-rollback",
                check=False,
            )
        await self._git_out(repo_dir, "reset", "--hard", rollback_sha)
        remotes = await self._git_out(repo_dir, "remote", check=False)
        if "origin" in remotes.splitlines():
            await self._git_out(
                repo_dir, "push", "origin", "HEAD:master", "--force-with-lease", check=False
            )
        if proposal_id is not None:
            self.db.set_harness_outcome(proposal_id=proposal_id, outcome="rolled_back")
        self.restart_pending = True
        return True

    async def _git_fetch(self, repo_dir: Path) -> None:
        remotes = await self._git_out(repo_dir, "remote")
        if "origin" in remotes.splitlines():
            await self._git_out(repo_dir, "fetch", "origin", "--prune", check=False)

    async def _prepare_worktree(
        self,
        repo_dir: Path,
        worktree_dir: Path,
        branch_name: str,
        *,
        prefer_local_head: bool = False,
    ) -> str:
        if worktree_dir.exists():
            await self._cleanup_worktree(repo_dir, worktree_dir)
        has_origin_master = (
            not prefer_local_head
            and await self._git_rc(repo_dir, "rev-parse", "--verify", "origin/master") == 0
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
        _clean_untracked_tool_caches(worktree_dir)
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
    async def _git_capture(cwd: Path, *args: str) -> tuple[int, str]:
        """Runs git and returns `(returncode, stdout+stderr)` for loud failure reporting."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(cwd),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        returncode = proc.returncode if proc.returncode is not None else 1
        return returncode, out.decode("utf-8", errors="replace").strip()

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
