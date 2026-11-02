from __future__ import annotations

import asyncio
import json
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
from jarvis.state import AgentTopic, JarvisState

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
        named = [o["value"] for o in options if re.search(rf"\b{re.escape(o['label'].lower())}\b", said)]
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
    type: str  # "claude_code" | "opencode_sse"
    cwd: str = "."
    url: str | None = None
    enabled: bool = True


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
            # File edits go through; Bash and other tools not allowed in the user's settings ask first.
            permission_mode="acceptEdits",
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
            self._running = False
            if self._stopped:
                self._stopped = False
                return
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

    async def answer(self, answer: str) -> bool:
        if self._question is None or self._question.done():
            return False
        self._question.set_result(answer)
        return True

    async def interrupt(self) -> bool:
        if not self._client:
            return False
        if self._question is not None and not self._question.done():
            self._question.set_result(None)
        self._stopped = self._running
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
            if self._pending_form and self._pending_form.get("id") == request_id:
                self._pending_form = None
        elif ev_type == "session.text.ended" and session_id:
            self._last_text[session_id] = data.get("text", "")
        elif ev_type == "session.execution.interrupted" and session_id:
            self._last_text.pop(session_id, None)
        elif ev_type in ("session.execution.succeeded", "session.execution.failed") and session_id:
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
            return resp.is_success
        except httpx.HTTPError as exc:
            logger.error(f"Failed to send task to OpenCode: {exc}")
            return False

    async def answer(self, answer: str) -> bool:
        try:
            if self._pending_permission is not None:
                request, self._pending_permission = self._pending_permission, None
                resp = await self._http.post(
                    f"/api/session/{request['sessionID']}/permission/{request['id']}/reply",
                    json={"decision": "once" if is_approval(answer) else "reject"},
                )
                return resp.is_success
            if self._pending_form is not None:
                form, self._pending_form = self._pending_form, None
                resp = await self._http.post(
                    f"/api/session/{form['sessionID']}/form/{form['id']}/reply",
                    json={"answer": form_answer(form.get("fields", []), answer)},
                )
                return resp.is_success
            return False
        except httpx.HTTPError as exc:
            logger.error(f"Failed to answer OpenCode: {exc}")
            return False

    async def interrupt(self) -> bool:
        if not self._session_id:
            return False
        try:
            resp = await self._http.post(f"/api/session/{self._session_id}/interrupt")
            return resp.is_success
        except httpx.HTTPError as exc:
            logger.error(f"Failed to interrupt OpenCode: {exc}")
            return False

    async def stop(self) -> None:
        await super().stop()
        await self._http.aclose()


class AgentOrchestrator:
    """Manages all coding agent watchers and routes user verbal decisions back to the right agent."""

    def __init__(self, state: JarvisState) -> None:
        self.state = state
        self.adapters: dict[str, BaseAgentAdapter] = {}

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

    @property
    def default_agent_id(self) -> str | None:
        """The configured default agent, or the first loaded one when DEFAULT_CODING_AGENT is empty."""
        return settings.default_coding_agent or next(iter(self.adapters), None)

    def find(self, agent_id: str | None) -> BaseAgentAdapter | None:
        """The agent with this exact id, or the default agent when no id is given."""
        target_id = agent_id or self.default_agent_id
        adapter = self.adapters.get(target_id) if target_id else None
        if adapter is None:
            logger.warning(f"No active agent '{target_id}'; known: {list(self.adapters)}")
        return adapter
