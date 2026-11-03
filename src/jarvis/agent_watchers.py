from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLINotFoundError,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    ToolPermissionContext,
)

from jarvis.config import settings
from jarvis.state import AgentTopic, JarvisMode, JarvisState

# Refusals are checked first, so "not sure" or "don't allow" never count as approval.
_REJECT_WORDS = re.compile(r"\b(no|not|don'?t|do not|never|reject|deny|denied|cancel|stop|wait)\b", re.IGNORECASE)
_APPROVE_WORDS = re.compile(r"\b(yes|yep|yeah|approve|approved|allow|go ahead|sure|do it|ok|okay|proceed)\b", re.IGNORECASE)


def _request_of(data: dict[str, Any]) -> dict[str, Any]:
    """OpenCode wraps requests in the event data (e.g. `data.form`); return the object holding id and sessionID."""
    return next((v for v in data.values() if isinstance(v, dict) and "id" in v and "sessionID" in v), data)


def form_answer(fields: list[dict[str, Any]], instruction: str) -> dict[str, Any]:
    """Map a spoken answer onto OpenCode form fields: a named option, yes/no, or the user's own words."""
    said = instruction.lower()
    answer: dict[str, Any] = {}
    for field in fields:
        if field.get("hidden"):
            continue
        options = field.get("options") or []
        # (?<!\w)/(?!\w), not \b: a label like "C++" or ".env" has no word edge at its punctuation.
        named = [o["value"] for o in options if re.search(rf"(?<!\w){re.escape(o['label'].lower())}(?!\w)", said)]
        if field.get("type") == "boolean":
            answer[field["key"]] = is_approval(instruction)
        elif field.get("type") == "multiselect":
            answer[field["key"]] = named or [instruction]
        else:
            answer[field["key"]] = named[0] if named else instruction
    return answer


def describe_fields(fields: list[dict[str, Any]]) -> str:
    """Speakable text for a list of questions: each question, then its options."""
    asks = []
    for field in fields:
        question = field.get("description") or field.get("title") or ""
        labels = [o["label"] for o in field.get("options") or []]
        asks.append(f"{question} Options: {', '.join(labels)}." if labels else question)
    return " ".join(asks)


def claude_question_fields(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Claude Code AskUserQuestion questions in the OpenCode form-field shape, so form_answer works for both."""
    return [
        {
            "key": q["question"],
            "description": q["question"],
            "type": "multiselect" if q.get("multiSelect") else "string",
            "options": [{"value": o["label"], "label": o["label"]} for o in q.get("options", [])],
        }
        for q in questions
    ]


def is_approval(instruction: str) -> bool:
    """True only when the user clearly approves and says nothing that sounds like a refusal."""
    if _REJECT_WORDS.search(instruction):
        return False
    return bool(_APPROVE_WORDS.search(instruction))


@dataclass
class WatchedAgentConfig:
    id: str
    name: str
    type: str  # "claude_code" | "opencode_sse" | "hermes"
    cwd: str = "."
    url: str | None = None
    enabled: bool = True
    role: str = ""  # what this agent is for; the conversation model routes by it


class BaseAgentAdapter:
    """Watches one coding agent. A new task, an answer to the agent's question, and a stop are separate calls."""

    def __init__(
        self,
        config: WatchedAgentConfig,
        state: JarvisState,
    ) -> None:
        self.config = config
        self.state = state
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop(), name=f"watcher-{self.config.id}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        raise NotImplementedError

    async def send_task(self, task: str) -> bool:
        """Send new work to the agent as a prompt."""
        raise NotImplementedError

    async def answer(self, answer: str) -> bool:
        """Answer the agent's pending question or permission request. False when nothing is pending."""
        raise NotImplementedError

    async def interrupt(self) -> bool:
        """Stop the agent's current turn."""
        raise NotImplementedError

    @property
    def busy(self) -> bool:
        """A turn is running, so a stop with no agent named goes here."""
        return False

    def _question_settled(self) -> None:
        """The agent's question was answered, cancelled or stopped: stop telling the speech check about it."""
        self.state.active_agent_context.pop(self.config.id, None)

    async def _surface(self, text: str, is_report: bool = False) -> None:
        """Tell the user: a question or approval is spoken as is; a finished turn (is_report) gets retold."""
        logger.info(f"[{self.config.name}] Surfaced: {text[:200]}")
        await self.state.notify_topic(
            AgentTopic(agent_id=self.config.id, agent_name=self.config.name, summary=text, is_report=is_report)
        )


class ClaudeCodeAdapter(BaseAgentAdapter):
    """
    Runs Claude Code through the Agent SDK, one long-lived process for the whole Jarvis run.
    Permission prompts and AskUserQuestion arrive in `can_use_tool`; the agent waits until `answer()`.
    """

    def __init__(
        self,
        config: WatchedAgentConfig,
        state: JarvisState,
    ) -> None:
        super().__init__(config, state)
        self._client: ClaudeSDKClient | None = None
        self._question: asyncio.Future[str | None] | None = None  # None result = stopped by the user
        self._ask_lock = asyncio.Lock()  # parallel tool calls are asked one at a time
        self._running = False  # a turn is in progress (a task was sent and its result has not arrived)
        self._stopped = False  # the user stopped this turn, so its error result is not news

    async def _run_loop(self) -> None:
        options = ClaudeAgentOptions(
            cwd=self.config.cwd,
            # No permission_mode: the user's own settings decide, the same as their Claude Code anywhere else.
            can_use_tool=self._can_use_tool,
            setting_sources=["user", "project", "local"],
            stderr=lambda line: logger.debug(f"[{self.config.id}] {line}"),
        )
        try:
            async with ClaudeSDKClient(options) as client:
                self._client = client
                logger.info(f"Started Claude Code [{self.config.id}] in {self.config.cwd}")
                async for message in client.receive_messages():
                    await self._handle_message(message)
        except CLINotFoundError:
            logger.warning(f"[{self.config.id}] `claude` CLI not found on PATH; watcher idle.")
        finally:
            self._client = None

    async def _handle_message(self, message: Any) -> None:
        if isinstance(message, ResultMessage):
            if self._stopped:  # the stopped turn's result; a task sent since then is still running
                self._stopped = False
                return
            self._running = False
            text = message.result or ("" if not message.is_error else f"The turn failed ({message.subtype}).")
            if text:
                await self._surface(text, is_report=True)

    async def _ask(self, spoken: str) -> str | None:
        async with self._ask_lock:
            self._question = asyncio.get_running_loop().create_future()
            try:
                await self._surface(spoken)
                return await self._question
            finally:
                self._question = None

    async def _can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name == "AskUserQuestion":
            fields = claude_question_fields(tool_input.get("questions", []))
            said = await self._ask(f"asks: {describe_fields(fields)}")
            if said is None:
                return PermissionResultDeny(message="Stopped by the user.", interrupt=True)
            answers = {k: ", ".join(v) if isinstance(v, list) else str(v) for k, v in form_answer(fields, said).items()}
            return PermissionResultAllow(updated_input={**tool_input, "answers": answers})

        what = context.title or tool_input.get("command") or context.description or tool_name
        said = await self._ask(f"needs approval: {what}")
        if said is None:
            return PermissionResultDeny(message="Stopped by the user.", interrupt=True)
        if is_approval(said):
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(message=f"The user declined: {said}")

    async def send_task(self, task: str) -> bool:
        if not self._client:
            logger.warning(f"[{self.config.id}] Cannot send task; Claude Code is not running.")
            return False
        self._running = True
        await self._client.query(task)
        logger.info(f"Sent task to {self.config.name}: {task!r}")
        return True

    @property
    def busy(self) -> bool:
        return self._running

    async def answer(self, answer: str) -> bool:
        if self._question is None or self._question.done():
            return False
        self._question.set_result(answer)
        self._question_settled()
        return True

    async def interrupt(self) -> bool:
        if not self._client:
            return False
        if self._question is not None and not self._question.done():
            self._question.set_result(None)
        self._question_settled()
        self._stopped, self._running = self._running, False
        await self._client.interrupt()
        return True


class OpenCodeSSEAdapter(BaseAgentAdapter):
    """
    Talks to an OpenCode v2 server (`opencode serve`): events over SSE at `/api/event`, actions over `/api`.
    Auth: HTTP Basic, user `opencode`, password from OPENCODE_SERVER_PASSWORD.
    """

    def __init__(
        self,
        config: WatchedAgentConfig,
        state: JarvisState,
    ) -> None:
        super().__init__(config, state)
        self.base_url = (config.url or "http://127.0.0.1:4096").rstrip("/")
        auth = ("opencode", settings.opencode_server_password) if settings.opencode_server_password else None
        self._http = httpx.AsyncClient(base_url=self.base_url, auth=auth, timeout=10.0)
        self._session_id: str | None = None  # latest session seen, or the one Jarvis created
        self._pending_permission: dict[str, Any] | None = None
        self._pending_form: dict[str, Any] | None = None  # a question OpenCode is waiting on
        self._spoken: set[str] = set()  # request ids already read out, so a resync doesn't repeat them
        self._last_text: dict[str, str] = {}  # session id -> latest finished assistant text
        self._running = False  # a task Jarvis sent has not finished yet
        self._stopped = False  # Jarvis stopped the turn; its interrupted event is not news

    async def _run_loop(self) -> None:
        while True:
            try:
                # The server sends a heartbeat every 15 s; 45 s of silence means the stream is dead.
                timeout = httpx.Timeout(10.0, read=45.0)
                async with self._http.stream("GET", "/api/event", timeout=timeout) as response:
                    response.raise_for_status()
                    logger.info(f"Connected to OpenCode event stream at {self.base_url}/api/event")
                    await self._resync()
                    async for line in response.aiter_lines():
                        raw_data = line.removeprefix("data:").strip() if line.startswith("data:") else ""
                        if not raw_data:
                            continue
                        try:
                            event = json.loads(raw_data)
                        except json.JSONDecodeError:
                            continue
                        await self._handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Reconnect if `opencode serve` is not started yet, dropped the stream, or went silent
                logger.debug(f"[{self.config.id}] OpenCode stream unavailable ({exc!r}); retrying in 10s.")
                await asyncio.sleep(10.0)

    async def _resync(self) -> None:
        """Events are not replayed, so pick up permissions and questions that were asked while disconnected."""
        for path, on_request in (("/api/permission/request", self._on_permission), ("/api/form", self._on_form)):
            resp = await self._http.get(path)
            resp.raise_for_status()
            for request in resp.json().get("data", []):
                await on_request(request)

    async def _on_permission(self, request: dict[str, Any]) -> None:
        self._pending_permission = request
        self._session_id = request.get("sessionID", self._session_id)
        if request["id"] not in self._spoken:
            self._spoken.add(request["id"])
            what = " ".join([request.get("action", "")] + request.get("resources", [])).strip()
            await self._surface(f"needs approval: {what or 'a tool call'}")

    async def _on_form(self, form: dict[str, Any]) -> None:
        self._pending_form = form
        self._session_id = form.get("sessionID", self._session_id)
        if form["id"] not in self._spoken:
            self._spoken.add(form["id"])
            await self._surface(f"asks: {describe_fields(form.get('fields', []))}")

    async def _handle_event(self, event: dict[str, Any]) -> None:
        ev_type = event.get("type", "")
        data = event.get("data") or {}
        session_id = data.get("sessionID")
        if session_id:
            self._session_id = session_id

        if ev_type == "permission.asked":
            await self._on_permission(_request_of(data))
        elif ev_type == "form.created":
            await self._on_form(_request_of(data))
        elif ev_type in ("permission.replied", "form.replied", "form.cancelled"):
            # Answered somewhere else (e.g. the OpenCode UI): stop holding it as pending.
            request_id = data.get("requestID") or _request_of(data).get("id")
            if self._pending_permission and self._pending_permission.get("id") == request_id:
                self._pending_permission = None
                self._question_settled()
            if self._pending_form and self._pending_form.get("id") == request_id:
                self._pending_form = None
                self._question_settled()
        elif ev_type == "session.text.ended" and session_id:
            self._last_text[session_id] = data.get("text", "")
        elif ev_type == "session.execution.interrupted" and session_id:
            if self._stopped:  # Jarvis stopped it; a task sent since then is still running
                self._stopped = False
            else:
                self._running = False  # stopped in the OpenCode UI
            self._last_text.pop(session_id, None)
        elif ev_type in ("session.execution.succeeded", "session.execution.failed") and session_id:
            self._running = False
            text = self._last_text.pop(session_id, "")
            if text:
                await self._surface(("The turn failed. " if ev_type.endswith("failed") else "") + text, is_report=True)

    async def send_task(self, task: str) -> bool:
        try:
            if not self._session_id:
                # No session seen yet: start one in the folder `opencode serve` runs in.
                resp = await self._http.post("/api/session", json={"title": "Jarvis"})
                resp.raise_for_status()
                self._session_id = resp.json()["data"]["id"]
            resp = await self._http.post(f"/api/session/{self._session_id}/prompt", json={"text": task})
            self._running = resp.is_success
            return resp.is_success
        except httpx.HTTPError as exc:
            logger.error(f"Failed to send task to OpenCode: {exc}")
            return False

    async def answer(self, answer: str) -> bool:
        try:
            # Clear a request only once OpenCode took the reply, so a network error leaves it answerable.
            if self._pending_permission is not None:
                request = self._pending_permission
                resp = await self._http.post(
                    f"/api/session/{request['sessionID']}/permission/{request['id']}/reply",
                    json={"decision": "once" if is_approval(answer) else "reject"},
                )
                if resp.is_success:
                    self._pending_permission = None
                    self._question_settled()
                return resp.is_success
            if self._pending_form is not None:
                form = self._pending_form
                resp = await self._http.post(
                    f"/api/session/{form['sessionID']}/form/{form['id']}/reply",
                    json={"answer": form_answer(form.get("fields", []), answer)},
                )
                if resp.is_success:
                    self._pending_form = None
                    self._question_settled()
                return resp.is_success
            return False
        except httpx.HTTPError as exc:
            logger.error(f"Failed to answer OpenCode: {exc}")
            return False

    @property
    def busy(self) -> bool:
        return self._running

    async def interrupt(self) -> bool:
        if not self._session_id:
            return False
        try:
            resp = await self._http.post(f"/api/session/{self._session_id}/interrupt")
            if resp.is_success:
                self._question_settled()
                self._stopped, self._running = self._running, False
            return resp.is_success
        except httpx.HTTPError as exc:
            logger.error(f"Failed to interrupt OpenCode: {exc}")
            return False

    async def stop(self) -> None:
        await super().stop()
        await self._http.aclose()


class HermesAdapter(BaseAgentAdapter):
    """
    Drives Hermes Agent through its API server (`hermes gateway`, API_SERVER_ENABLED=true).
    Each task is a run in one ongoing Hermes session. Hermes's own guardian model decides which commands
    need a human; those arrive as `approval.request`. A question Hermes asks ends its turn, so the user's
    reply goes back as the next task in the same session.
    """

    _DONE = {"run.completed", "run.failed", "run.cancelled"}

    def __init__(self, config: WatchedAgentConfig, state: JarvisState) -> None:
        super().__init__(config, state)
        self.base_url = (config.url or "http://127.0.0.1:8642").rstrip("/")
        headers = {"Authorization": f"Bearer {settings.hermes_api_key}"} if settings.hermes_api_key else {}
        self._http = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=10.0)
        self._session_id: str | None = None  # Hermes names a session after its first run
        self._run_id: str | None = None  # the run in progress
        self._runs: asyncio.Queue[str] = asyncio.Queue()
        self._pending_approval: dict[str, Any] | None = None

    async def _run_loop(self) -> None:
        while True:
            run_id = await self._runs.get()
            self._run_id = run_id  # set and cleared only here, so a queued task can't be wiped by the run before it
            while not await self._watch(run_id):
                await asyncio.sleep(5.0)  # stream dropped mid-run; the run goes on in Hermes, so look again
            self._run_id = None

    async def _watch(self, run_id: str) -> bool:
        """Follow one run's events; True once it finished. On a dropped stream, ask the run's status instead."""
        try:
            async with self._http.stream("GET", f"/v1/runs/{run_id}/events", timeout=httpx.Timeout(10.0, read=None)) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line.removeprefix("data:"))
                    except json.JSONDecodeError:
                        continue
                    await self._handle_event(event)
                    if event.get("event") in self._DONE:
                        return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"[{self.config.id}] Hermes event stream for {run_id} dropped ({exc!r}).")
        try:
            resp = await self._http.get(f"/v1/runs/{run_id}")
            resp.raise_for_status()
            run = resp.json()
        except httpx.HTTPError:
            return False
        if run.get("status") in ("completed", "failed", "cancelled"):
            await self._handle_event({**run, "event": f"run.{run['status']}"})
            return True
        return False

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("event", "")
        if kind == "approval.request":
            self._pending_approval = event
            what = event.get("command") or event.get("description") or "a tool call"
            await self._surface(f"needs approval: {what}")
        elif kind == "approval.responded":
            self._pending_approval = None  # answered here or in another Hermes client
            self._question_settled()
        elif kind == "run.completed" and event.get("output"):
            await self._surface(event["output"], is_report=True)
        elif kind == "run.failed":
            await self._surface(f"The turn failed. {event.get('error', '')}".strip(), is_report=True)
        # run.cancelled: the user stopped it, so it is not news.

    async def send_task(self, task: str) -> bool:
        body: dict[str, Any] = {"input": task}
        if self._session_id:
            body["session_id"] = self._session_id
        try:
            resp = await self._http.post("/v1/runs", json=body)
            resp.raise_for_status()
            run_id = resp.json()["run_id"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.error(f"Failed to send task to Hermes: {exc}")
            return False
        self._session_id = self._session_id or run_id  # a new session is keyed by its first run
        await self._runs.put(run_id)
        return True

    async def answer(self, answer: str) -> bool:
        if self._pending_approval is None:
            return False
        request = self._pending_approval
        body: dict[str, Any] = {"choice": "once" if is_approval(answer) else "deny"}
        if request.get("request_id"):
            body["request_id"] = request["request_id"]
        try:
            resp = await self._http.post(f"/v1/runs/{request['run_id']}/approval", json=body)
            if resp.is_success:  # cleared only once Hermes took it, so a network error leaves it answerable
                self._pending_approval = None
                self._question_settled()
            return resp.is_success
        except httpx.HTTPError as exc:
            logger.error(f"Failed to answer Hermes: {exc}")
            return False

    @property
    def busy(self) -> bool:
        return self._run_id is not None or not self._runs.empty()

    async def interrupt(self) -> bool:
        """Stop the run in progress and every run queued behind it (Hermes queues them per session)."""
        queued = []
        while not self._runs.empty():
            queued.append(self._runs.get_nowait())
        run_ids = [r for r in (self._run_id, *queued) if r]
        if not run_ids:
            return False
        stopped = False
        for run_id in run_ids:
            try:
                resp = await self._http.post(f"/v1/runs/{run_id}/stop")
                stopped = stopped or resp.is_success
            except httpx.HTTPError as exc:
                logger.error(f"Failed to stop Hermes run {run_id}: {exc}")
        if stopped:
            self._question_settled()
        return stopped

    async def stop(self) -> None:
        await super().stop()
        await self._http.aclose()


# Fixed lines, no LLM: free, instant, and nothing an agent wrote can slip in. {name} is the agent.
WAITING_LINES = (
    "Still waiting on {name}.",
    "{name} is still on it.",
    "Still working on it. {name} hasn't come back yet.",
    "Hang tight, {name} is still digging.",
    "No news from {name} yet. I haven't forgotten.",
    "{name} is still busy. I'm keeping an eye on it.",
    "Still here, still waiting on {name}.",
    "{name} is taking its time. Good work usually does.",
    "Still waiting. If {name} were a kettle, it'd be almost boiling.",
    "{name} is thinking hard. I can almost hear the fans.",
    "Still on it. I'd make coffee, but I don't have hands.",
    "{name} is still going. Patience is my only skill that never crashes.",
)


class AgentOrchestrator:
    """Manages all coding agent watchers and routes user verbal decisions back to the right agent."""

    def __init__(self, state: JarvisState) -> None:
        self.state = state
        self.adapters: dict[str, BaseAgentAdapter] = {}
        self._reminders: dict[str, asyncio.Task] = {}

    def load_from_file(self, config_path: str | Path) -> None:
        path = Path(config_path)
        if not path.exists():
            logger.warning(f"Agent config file {path} not found.")
            return

        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data.get("agents", []):
            cfg = WatchedAgentConfig(**item)
            if not cfg.enabled:
                continue
            if cfg.type == "claude_code":
                self.adapters[cfg.id] = ClaudeCodeAdapter(cfg, self.state)
            elif cfg.type == "opencode_sse":
                self.adapters[cfg.id] = OpenCodeSSEAdapter(cfg, self.state)
            elif cfg.type == "hermes":
                self.adapters[cfg.id] = HermesAdapter(cfg, self.state)
            else:
                logger.warning(f"Agent {cfg.id!r} has unknown type {cfg.type!r}; skipped.")

        default = settings.default_coding_agent
        if default and default not in self.adapters:
            raise ValueError(f"DEFAULT_CODING_AGENT={default!r} is not an enabled agent id; use one of {list(self.adapters)}")

    async def start_all(self) -> None:
        for adapter in self.adapters.values():
            await adapter.start()

    async def stop_all(self) -> None:
        for adapter in self.adapters.values():
            await adapter.stop()

    def remind_while_busy(self, agent: BaseAgentAdapter) -> None:
        """After a task is sent: say "Still waiting on X" while X works, so the user knows it isn't forgotten."""
        old = self._reminders.pop(agent.config.id, None)
        if old:
            old.cancel()  # a new task restarts the count
        self._reminders[agent.config.id] = asyncio.create_task(self._remind(agent))

    async def _remind(self, agent: BaseAgentAdapter) -> None:
        wait, next_wait = 8, 13  # Fibonacci seconds: 8, 13, 21, 34, 55, 89, 144, ...
        last = None
        while True:
            await asyncio.sleep(wait)
            if not agent.busy:
                return
            waiting_on_user = agent.config.id in self.state.active_agent_context  # it asked something
            if not waiting_on_user and self.state.mode == JarvisMode.VOICE and self.state.on_speak:
                last = random.choice([t for t in WAITING_LINES if t != last])  # never the same twice in a row
                await self.state.on_speak(last.format(name=agent.config.name))
            wait, next_wait = next_wait, wait + next_wait

    @property
    def default_agent_id(self) -> str | None:
        """The configured default agent, or the first loaded one when DEFAULT_CODING_AGENT is empty."""
        return settings.default_coding_agent or next(iter(self.adapters), None)

    def find_waiting(self, agent_id: str | None) -> BaseAgentAdapter | None:
        """For an answer: the named agent, else the one that asked last, else the default."""
        if not agent_id:
            asked = [a for a in reversed(list(self.state.active_agent_context)) if a in self.adapters]
            agent_id = asked[0] if asked else None
        return self.find(agent_id)

    def find_busy(self, agent_id: str | None) -> BaseAgentAdapter | None:
        """For a stop: the named agent, else one with a running turn, else the default."""
        if not agent_id:
            agent_id = next((a.config.id for a in self.adapters.values() if a.busy), None)
        return self.find(agent_id)

    def find(self, agent_id: str | None) -> BaseAgentAdapter | None:
        """The agent with this exact id, or the default agent when no id is given."""
        target_id = agent_id or self.default_agent_id
        adapter = self.adapters.get(target_id) if target_id else None
        if adapter is None:
            logger.warning(f"No active agent '{target_id}'; known: {list(self.adapters)}")
        return adapter
