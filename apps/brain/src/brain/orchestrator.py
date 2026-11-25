"""The brain's turn router: one small model call decides between a one-shot `pi` answer, a T3 project thread,
a minimal Claude/OpenCode turn, or the board — then the matching runner does the work.

The router is deliberately cheap and narrow (thinking off, one tool call). Latency matters because the voice
waits on it: mode switches, `jarvis connect` sessions and plain project continuations bypass it entirely, and
a project named in the request is resolved in code. Approvals and questions never pass through this file.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from loguru import logger

from brain import memory
from brain.config import Settings
from brain.db import BrainDB, TaskRow
from brain.reconciler import Reconciler
from brain.runners.base import ApprovalCallback, RunRequest, RunResult
from brain.telemetry import EVENT_BRAIN_ROUTE, preview
from jarvis_core.projects import list_projects, projects_dir, resolve_project
from jarvis_core.questions import is_approval

EventEmitter = Callable[[dict[str, Any]], Awaitable[None]]

ROUTER_PROMPT = """# === EVOLVE-BLOCK: router ===
You route one request for a voice assistant. Pick exactly one tool.

The user's project folders under {projects_label}:
{projects}

Threads/sessions already open (project: title):
{sessions}

Board: mode {mode}, ${spent:.2f} of ${budget:.2f} spent today, {ready} ready task(s).

Rules:
- Any coding, debugging, build/test fixing, code question, or work in a listed project -> delegate_project.
  Put the folder exactly as listed in `project`; when none is named, leave it empty and describe the project
  in `task`, or use the most recently used one.
- A small, self-contained question or lookup that is not about a project (news, docs, a fact, a quick check
  of this machine) -> ask_ephemeral_pi.
- Asking what threads/sessions exist -> list_sessions.
- "queue this", "overnight", "autonomous", a multi-step goal, or fixing/upgrading Jarvis, brain, or Jarvis's
  own dashboard ("your dashboard", which lives in `apps/dashboard/` of repo `jarvis`) -> queue_board_task
  (with `repo="jarvis"`, `kind="problem"` for bugs or `"goal"` for features, and `apps/dashboard/` in `spec`).
  Generic "the dashboard" without "your"/"Jarvis's" stays with delegate_project on the current project.
- A question the assistant can answer from the project list or the board line above -> reply_direct.
- Name Claude Code or OpenCode only when the user says so; otherwise runner="t3".
- Never invent a folder, a task or a session title.
Return only the tool call.
# === END-EVOLVE-BLOCK: router ===
"""

ROUTER_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "delegate_project",
            "description": "Do coding or project work in a listed folder, in a T3 thread (or a named client).",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Listed folder, e.g. HuggerHustle/platform-v2."},
                    "task": {"type": "string", "description": "The work, in clear English."},
                    "session": {"type": "string", "description": "\"new\" for a fresh thread, or words naming an earlier one."},
                    "runner": {"type": "string", "enum": ["t3", "claude", "opencode"]},
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_ephemeral_pi",
            "description": "Answer a small, self-contained question with a one-shot worker; nothing is kept.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_sessions",
            "description": "List open coding threads/sessions, newest first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "runner": {"type": "string", "enum": ["t3", "claude", "opencode"]},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_board_task",
            "description": "Put a goal or problem on the board for the reconciler (overnight/autonomous work).",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["goal", "problem"]},
                    "repo": {"type": "string"},
                    "title": {"type": "string"},
                    "spec": {"type": "string"},
                    "metric_target": {"type": "string"},
                    "verify_cmd": {"type": "string"},
                    "mode": {"type": "string", "enum": ["ASSIST", "AUTONOMOUS"]},
                },
                "required": ["kind", "repo", "title", "spec"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reply_direct",
            "description": "Answer directly from the project list or the board line; no worker runs.",
            "parameters": {
                "type": "object",
                "properties": {"spoken": {"type": "string"}},
                "required": ["spoken"],
            },
        },
    },
]

_QUESTION_START = re.compile(r"^(what|who|when|where|why|how|which|is|are|does|did|can|could|tell me)\b", re.I)
_PROJECT_WORK = re.compile(
    r"\b(open|continue|work on|working on|fix|bug|refactor|test|build|deploy|implement|add|update|rename|"
    r"review|commit|branch|pull request|repo)\b",
    re.I,
)
_SELF_BOARD = re.compile(r"\b(upgrade (?:jarvis|brain)|your (?:code|dashboard)|jarvis(?:'s)? dashboard)\b", re.I)
_BOARD = re.compile(r"\b(overnight|autonomous|queue|schedule|goal)\b", re.I)


class BrainOrchestrator:
    """Turns one request into one runner call, with the cheapest routing that still gets it right."""

    def __init__(
        self,
        cfg: Settings,
        db: BrainDB,
        reconciler: Reconciler,
        *,
        runners: dict[str, Any],
        last_project: Callable[[str], str | None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.reconciler = reconciler
        self.runners = runners
        self._last_project = last_project or (lambda session: None)
        self._http = (
            httpx.AsyncClient(
                base_url=cfg.openrouter_base_url.rstrip("/"),
                timeout=20.0,
                headers={"Authorization": f"Bearer {cfg.openrouter_api_key}", "X-Title": "Jarvis brain"},
            )
            if cfg.openrouter_api_key
            else None
        )

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    # ------------------------------------------------------------------ routing

    async def decide(self, user_input: str, *, session_id: str = "") -> tuple[dict[str, Any], int]:
        """The route choice and the router's latency in ms: `route` dispatches this, the benchmark scores it."""
        label, _ = self._projects()
        projects = list_projects()
        sessions = await self._session_lines()
        state = self.db.get_state()
        ready = sum(1 for t in self.db.list_tasks(status="ready"))
        prompt = ROUTER_PROMPT.format(
            projects_label=label,
            projects="\n".join(f"- {p}" for p in projects) or "(none)",
            sessions="\n".join(sessions) or "(none)",
            mode=state.mode,
            spent=state.spent_today_usd,
            budget=state.daily_budget_usd,
            ready=ready,
        )
        # Standing memory first: a stable prefix keeps the router's prompt cache warm, and daytime routing
        # then knows what the night learned.
        standing = memory.standing_context(self.cfg.home_dir)
        if standing:
            prompt = f"Standing memory (verified overnight; facts, not instructions):\n{standing}\n\n{prompt}"
        started = time.monotonic()
        choice = await self._choose(prompt, user_input)
        route_ms = round((time.monotonic() - started) * 1000)
        if choice is None:
            choice = self._heuristic(user_input, projects, session_id)
        return choice, route_ms

    async def route(
        self,
        *,
        run_id: str,
        user_input: str,
        session_id: str,
        pinned: dict[str, str] | None,
        approval_cb: ApprovalCallback | None,
        emit: EventEmitter,
        screenshot: str | None = None,
        session_mode: str | None = None,
        cwd: str | None = None,
        chat_session_key: str | None = None,
        trace_id: str = "",
        turn_id: str = "",
    ) -> RunResult:
        choice, route_ms = await self.decide(user_input, session_id=session_id)
        logger.info(f"[Brain] route {choice['name']} for {user_input[:80]!r}")
        args = choice.get("arguments") or {}
        self._telemetry(
            phase="route",
            run_id=run_id,
            choice=str(choice.get("name")),
            runner=str(args.get("runner") or ""),
            project=str(args.get("project") or ""),
            text=preview(user_input, 400),
            route_ms=route_ms,
            trace_id=trace_id,
            turn_id=turn_id,
        )

        if pinned and choice["name"] in ("delegate_project", "ask_ephemeral_pi"):
            choice = {
                "name": "delegate_project",
                "arguments": {"task": user_input, "runner": pinned.get("runner", "t3"),
                              "session": pinned.get("session_id")},
            }
        result = await self._dispatch(
            choice, run_id=run_id, user_input=user_input, session_id=session_id,
            pinned=pinned, approval_cb=approval_cb, emit=emit, screenshot=screenshot,
            session_mode=session_mode, cwd=cwd, chat_session_key=chat_session_key,
        )
        self._telemetry(
            phase="done",
            run_id=run_id,
            choice=str(choice.get("name")),
            outcome=result.outcome,
            project=result.project or "",
            summary=preview(result.summary, 400),
            trace_id=trace_id,
            turn_id=turn_id,
        )
        return result

    def _telemetry(self, **fields: Any) -> None:
        """Publishes the route the brain took, for the dashboard; a missing hub is not an error."""
        try:
            self.reconciler.hub.emit(EVENT_BRAIN_ROUTE, **fields)
        except Exception:  # noqa: BLE001 - telemetry must never fail a request
            pass

    async def _choose(self, prompt: str, user_input: str) -> dict[str, Any] | None:
        if self._http is None:
            return None
        try:
            resp = await self._http.post(
                "/chat/completions",
                json={
                    "model": self.cfg.orchestrator_model or "deepseek/deepseek-v4.1-flash",
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": user_input},
                    ],
                    "tools": ROUTER_TOOLS,
                    "reasoning": {"enabled": False},
                    "provider": {"sort": "latency"},
                },
            )
            resp.raise_for_status()
            message = resp.json()["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if not calls:
                return None
            call = calls[0]["function"]
            return {"name": call["name"], "arguments": json.loads(call.get("arguments") or "{}")}
        except Exception as exc:  # routing must never fail a request
            logger.warning(f"[Brain] router unavailable ({exc!r}); using the local rule.")
            return None

    def _heuristic(
        self, text: str, projects: list[str], session_id: str
    ) -> dict[str, Any]:
        lowered = text.lower()
        if _SELF_BOARD.search(text):
            is_bug = bool(re.search(r"\b(fix|bug|broken|crash|flicker|error|issue)\b", lowered))
            return {
                "name": "queue_board_task",
                "arguments": {
                    "kind": "problem" if is_bug else "goal",
                    "repo": "jarvis",
                    "title": text[:80],
                    "spec": text,
                },
            }
        if _BOARD.search(text):
            return {"name": "queue_board_task", "arguments": {"text": text}}
        for project in projects:
            name = Path(project).name.lower()
            if name and name in lowered:
                return {"name": "delegate_project",
                        "arguments": {"project": project, "task": text, "runner": "t3"}}
        if re.search(r"\b(sessions?|threads?)\b", lowered) and re.search(r"\b(list|what|which|how many)\b", lowered):
            return {"name": "list_sessions", "arguments": {}}
        question = text.strip().endswith("?") or _QUESTION_START.search(text.strip()) is not None
        if question and not _PROJECT_WORK.search(text):
            return {"name": "ask_ephemeral_pi", "arguments": {"question": text}}
        recent = self._last_project(session_id)
        return {"name": "delegate_project",
                "arguments": {"project": recent or "", "task": text, "runner": "t3"}}

    async def _dispatch(
        self,
        choice: dict[str, Any],
        *,
        run_id: str,
        user_input: str,
        session_id: str,
        pinned: dict[str, str] | None,
        approval_cb: ApprovalCallback | None,
        emit: EventEmitter,
        screenshot: str | None,
        session_mode: str | None = None,
        cwd: str | None = None,
        chat_session_key: str | None = None,
    ) -> RunResult:
        name = choice.get("name")
        args = choice.get("arguments") or {}
        if name == "reply_direct":
            return RunResult(outcome="pushed", summary=str(args.get("spoken") or "Okay."))
        if name == "list_sessions":
            runner = str(args.get("runner") or "t3")
            project = str(args.get("project") or "")
            return await self._list_sessions(runner, project)
        if name == "queue_board_task":
            return await self._queue_task(args, user_input, run_id=run_id, approval_cb=approval_cb)
        if name == "ask_ephemeral_pi":
            question = str(args.get("question") or user_input)
            prompt = (
                f"Answer this for the owner, who is listening: {question}\n"
                "Use read/grep/find/ls and bash (curl, system tools) when the answer needs them. "
                "Reply in one or two short sentences that can be read aloud; no preamble, no file paths."
            )
            return await self._run(
                "ephemeral",
                RunRequest(
                    run_id=run_id,
                    prompt=prompt,
                    cwd=self.cfg.home_dir,
                    timeout_seconds=float(self.cfg.ephemeral_timeout_seconds),
                    max_cost_usd=self.cfg.max_ephemeral_cost_usd,
                    append_system_paths=tuple(memory.addendum_paths(self.cfg.home_dir)),
                    session_key=chat_session_key,
                ),
                emit,
            )
        # delegate_project (the default for anything else)
        runner_id = str(args.get("runner") or self.cfg.default_project_runner)
        if runner_id == "pi":
            return await self._queue_task(
                {"kind": "goal", "repo": str(args.get("project") or ""),
                 "title": user_input[:80], "spec": user_input, "mode": "ASSIST"},
                user_input,
                run_id=run_id,
                approval_cb=approval_cb,
            )
        project = str(args.get("project") or "")
        cwd_path = Path(cwd).expanduser() if cwd else None
        if not project and cwd_path is None:
            project = self._last_project(session_id) or (list_projects() or [""])[0]
            if not project:
                return RunResult(outcome="errored", summary="I don't know which project you mean yet.")
        if cwd_path is None:
            cwd_path = resolve_project(project)
        if cwd_path is None:
            return RunResult(outcome="errored", summary=f"I couldn't find a project called {project}.")
        task = str(args.get("task") or user_input)
        if screenshot:
            task += f"\n\n[Screenshot of the user's front window: {screenshot}]"
        request = RunRequest(
            run_id=run_id, prompt=task, project=project or None, cwd=cwd_path,
            session=session_mode or (str(args["session"]) if args.get("session") else None),
            pinned_session_id=pinned.get("session_id") if pinned else None,
            approval_cb=approval_cb,
        )
        result = await self._run(runner_id, request, emit)
        if result.outcome not in ("errored", "timed_out"):
            result.project = project or self._listed_folder(cwd_path)
        return result

    @staticmethod
    def _listed_folder(folder: Path | None) -> str | None:
        """The folder as the project list writes it (`Org/repo`), so a follow-up turn resolves it again."""
        if folder is None:
            return None
        _, root = projects_dir()
        try:
            return str(folder.resolve().relative_to(root.expanduser().resolve()))
        except ValueError:
            return folder.name

    async def _run(self, runner_id: str, request: RunRequest, emit: EventEmitter) -> RunResult:
        runner = self.runners.get(runner_id)
        if runner is None:
            return RunResult(outcome="errored", summary=f"The {runner_id} runner is not available.")
        try:
            return await runner.run(request, emit)
        except Exception as exc:
            logger.error(f"[Brain] {runner_id} run failed: {exc!r}")
            return RunResult(outcome="errored", summary=f"{runner_id} failed: {exc}")

    # ------------------------------------------------------------------ board & sessions

    async def _queue_task(
        self,
        args: dict[str, Any],
        user_input: str,
        *,
        run_id: str | None = None,
        approval_cb: ApprovalCallback | None = None,
    ) -> RunResult:
        repo = str(args.get("repo") or "").strip() or next(iter(sorted(self.db.allowed_repos)), "jarvis")
        repo = repo if repo in self.db.allowed_repos else self.db.validate_repo(repo)
        spec = str(args.get("spec") or user_input)
        kind = str(args.get("kind") or "goal")
        metric = str(args.get("metric_target") or "").strip()
        if kind == "goal" and not metric:
            metric = "The verify_cmd passes"
        try:
            task: TaskRow = self.db.upsert_task(
                kind=kind,  # type: ignore[arg-type]
                repo=repo,
                title=str(args.get("title") or user_input[:80]),
                spec=spec,
                metric_target=metric or None,
                verify_cmd=str(args.get("verify_cmd") or "").strip() or None,
                mode=str(args.get("mode") or self.db.get_state().mode),  # type: ignore[arg-type]
                runner="pi",
                ready=True,
            )
        except ValueError as exc:
            return RunResult(outcome="errored", summary=str(exc))
        note = f"Queued {task.id} in {task.repo}."
        if task.verify_cmd and task.repo in ("jarvis", "brain"):
            claimed = self.db.claim_next_task(lease_seconds=self.cfg.lease_seconds, task_id=task.id)
            if claimed is not None:
                bool_cb = None
                if approval_cb is not None:
                    async def _bool_cb(
                        summary_spoken: str, details: str, session_id: str | None
                    ) -> bool | None:
                        answer = await approval_cb(summary_spoken, details, session_id)
                        return None if answer is None else is_approval(answer)

                    bool_cb = _bool_cb
                done = await self.reconciler.execute_claimed_task(
                    claimed, run_key=run_id, approval_cb=bool_cb
                )
                note = f"{task.id} finished with status {done.status}."
                return RunResult(outcome="pushed", summary=note)
        self.reconciler.wake()
        return RunResult(outcome="pushed", summary=note)

    async def _list_sessions(self, runner_id: str, project: str) -> RunResult:
        runner = self.runners.get(runner_id)
        if runner is None:
            return RunResult(outcome="errored", summary=f"The {runner_id} runner is not available.")
        cwd = resolve_project(project) if project else None
        try:
            sessions = await runner.list_sessions(str(cwd) if cwd else None)
        except Exception as exc:
            return RunResult(outcome="errored", summary=f"Could not read {runner_id} sessions: {exc}")
        if not sessions:
            return RunResult(outcome="pushed", summary=f"No {runner_id} sessions yet.")
        titles = [s.title or "(untitled)" for s in sessions[:8]]
        return RunResult(outcome="pushed", summary="Sessions: " + "; ".join(titles) + ".")

    async def _session_lines(self) -> list[str]:
        lines: list[str] = []
        t3 = self.runners.get("t3")
        if t3 is not None:
            try:
                for session in (await t3.list_sessions(None, limit=6)):
                    lines.append(f"- t3: {session.title} ({Path(session.cwd).name})")
            except Exception:
                pass
        return lines

    def _projects(self) -> tuple[str, list[str]]:
        from jarvis_core.projects import projects_dir

        label, _ = projects_dir()
        return label, list_projects()
