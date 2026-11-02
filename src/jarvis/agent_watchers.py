from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from jarvis import __version__
from jarvis.config import settings
from jarvis.executive_filter import ExecutiveFilter
from jarvis.state import JarvisState

# Refusals are checked first, so "not sure" or "don't allow" never count as approval.
_REJECT_WORDS = re.compile(r"\b(no|not|don'?t|do not|never|reject|deny|denied|cancel|stop|wait)\b", re.IGNORECASE)
_APPROVE_WORDS = re.compile(r"\b(yes|yep|yeah|approve|approved|allow|go ahead|sure|do it|ok|okay|proceed)\b", re.IGNORECASE)


def is_approval(instruction: str) -> bool:
    """True only when the user clearly approves and says nothing that sounds like a refusal."""
    if _REJECT_WORDS.search(instruction):
        return False
    return bool(_APPROVE_WORDS.search(instruction))


@dataclass
class WatchedAgentConfig:
    id: str
    name: str
    type: str  # "claude_code" | "opencode_sse" | "acp_stdio"
    cwd: str = "."
    command: list[str] | None = None
    url: str | None = None
    enabled: bool = True


class BaseAgentAdapter:
    """Base interface for watching a coding agent and sending user decisions back to it."""

    def __init__(
        self,
        config: WatchedAgentConfig,
        state: JarvisState,
        executive_filter: ExecutiveFilter,
    ) -> None:
        self.config = config
        self.state = state
        self.executive_filter = executive_filter
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

    async def send_instruction(self, instruction: str) -> bool:
        """Send a natural-language decision or prompt back to this coding agent."""
        raise NotImplementedError

    async def _handle_raw_event(self, text: str, force_decision: bool = False) -> None:
        topic = await self.executive_filter.evaluate_agent_event(
            agent_id=self.config.id,
            agent_name=self.config.name,
            raw_event_text=text,
            force_decision=force_decision,
        )
        if topic is not None:
            logger.info(f"[{self.config.name}] Surfaced topic: {topic.summary}")
            await self.state.notify_topic(topic)


class ClaudeCodeStreamAdapter(BaseAgentAdapter):
    """
    Watches and controls a Claude Code instance via `--output-format stream-json --input-format stream-json`.
    Surfaces questions and final turn results through the ExecutiveFilter.
    shortcut: tool permission prompts are not answered; run it with `--permission-mode acceptEdits`
    (see agents.json) until Jarvis handles Claude Code's control_request messages.
    """

    def __init__(
        self,
        config: WatchedAgentConfig,
        state: JarvisState,
        executive_filter: ExecutiveFilter,
    ) -> None:
        super().__init__(config, state, executive_filter)
        self._proc: asyncio.subprocess.Process | None = None

    async def _run_loop(self) -> None:
        cmd = self.config.command or [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "acceptEdits",
        ]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.config.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # Nothing reads stderr; a full pipe would freeze the agent.
                stderr=asyncio.subprocess.DEVNULL,
            )
            logger.info(f"Started Claude Code stream watcher [{self.config.id}]: {' '.join(cmd)}")
        except FileNotFoundError:
            logger.warning(f"[{self.config.id}] `claude` CLI not found on PATH; watcher idle.")
            return

        assert self._proc.stdout is not None
        async for raw_line in self._proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            await self._handle_event(event)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "")
        if event_type == "result":
            await self._handle_raw_event(event.get("result", ""), force_decision=bool(event.get("is_error", False)))
        elif event_type == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text" and "?" in block.get("text", ""):
                    await self._handle_raw_event(block["text"], force_decision=True)

    async def send_instruction(self, instruction: str) -> bool:
        if not self._proc or not self._proc.stdin:
            logger.warning(f"[{self.config.id}] Cannot send instruction; process not running.")
            return False
        payload = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": instruction}]},
        }
        self._proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()
        logger.info(f"Sent instruction to {self.config.name}: {instruction!r}")
        return True


class ACPStdioAdapter(BaseAgentAdapter):
    """
    Watches and orchestrates Agent Client Protocol agents (Pi, Gemini CLI, etc.) over JSON-RPC 2.0 stdio.
    Handshake: `initialize`, then `session/new`; a turn ends with the response to `session/prompt`.
    """

    def __init__(
        self,
        config: WatchedAgentConfig,
        state: JarvisState,
        executive_filter: ExecutiveFilter,
    ) -> None:
        super().__init__(config, state, executive_filter)
        self._proc: asyncio.subprocess.Process | None = None
        self._rpc_id: int = 0
        self._requests: dict[int, str] = {}  # our request id -> method, to match responses
        self._session_id: str | None = None
        self._pending_permission: dict[str, Any] | None = None  # the agent's request, until answered
        self._text: list[str] = []

    async def _write(self, message: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any]) -> None:
        self._rpc_id += 1
        self._requests[self._rpc_id] = method
        await self._write({"jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params})

    async def _run_loop(self) -> None:
        cmd = self.config.command or ["pi", "--acp"]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.config.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # Nothing reads stderr; a full pipe would freeze the agent.
                stderr=asyncio.subprocess.DEVNULL,
            )
            logger.info(f"Started ACP stdio watcher [{self.config.id}]: {' '.join(cmd)}")
        except FileNotFoundError:
            logger.warning(f"[{self.config.id}] Command {cmd[0]!r} not found on PATH; watcher idle.")
            return

        await self._request(
            "initialize",
            {"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": {"name": "jarvis", "version": __version__}},
        )
        await self._request("session/new", {"cwd": str(Path(self.config.cwd).resolve()), "mcpServers": []})

        assert self._proc.stdout is not None
        async for raw_line in self._proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            await self._handle_message(msg)

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        params = msg.get("params", {})

        # Response to one of our requests
        if method is None and "id" in msg:
            sent = self._requests.pop(msg["id"], None)
            result = msg.get("result") or {}
            if "error" in msg:
                logger.warning(f"[{self.config.id}] {sent} failed: {msg['error']}")
            elif sent == "session/new":
                self._session_id = result.get("sessionId")
                logger.info(f"[{self.config.id}] ACP session ready: {self._session_id}")
            if sent == "session/prompt" and self._text:
                full_turn = "".join(self._text)
                self._text.clear()
                await self._handle_raw_event(full_turn, force_decision=result.get("stopReason") != "end_turn")
            return

        if method == "session/request_permission":
            self._pending_permission = msg
            title = params.get("toolCall", {}).get("title") or "a tool call"
            await self._handle_raw_event(f"Permission requested: {title}", force_decision=True)
        elif method == "session/update":
            update = params.get("update", {})
            if update.get("sessionUpdate") == "agent_message_chunk":
                self._text.append(update.get("content", {}).get("text", ""))
        elif "id" in msg:
            # We declare no client capabilities, so any other agent request gets "method not found".
            await self._write(
                {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"{method} not supported"}}
            )

    async def send_instruction(self, instruction: str) -> bool:
        if not self._proc or not self._proc.stdin:
            return False

        if self._pending_permission is not None:
            request, self._pending_permission = self._pending_permission, None
            wanted = ("allow_once", "allow_always") if is_approval(instruction) else ("reject_once", "reject_always")
            options = request.get("params", {}).get("options", [])
            option = next((o for kind in wanted for o in options if o.get("kind") == kind), None)
            outcome = {"outcome": "selected", "optionId": option["optionId"]} if option else {"outcome": "cancelled"}
            await self._write({"jsonrpc": "2.0", "id": request["id"], "result": {"outcome": outcome}})
            return True

        if not self._session_id:
            logger.warning(f"[{self.config.id}] Cannot send instruction; ACP session not ready.")
            return False
        await self._request(
            "session/prompt",
            {"sessionId": self._session_id, "prompt": [{"type": "text", "text": instruction}]},
        )
        return True


class OpenCodeSSEAdapter(BaseAgentAdapter):
    """
    Subscribes to an OpenCode v2 server (`opencode serve`) over Server-Sent Events at `/api/event`.
    Auth: HTTP Basic, user `opencode`, password from OPENCODE_SERVER_PASSWORD.
    """

    def __init__(
        self,
        config: WatchedAgentConfig,
        state: JarvisState,
        executive_filter: ExecutiveFilter,
    ) -> None:
        super().__init__(config, state, executive_filter)
        self.base_url = (config.url or "http://127.0.0.1:4096").rstrip("/")
        auth = ("opencode", settings.opencode_server_password) if settings.opencode_server_password else None
        self._http = httpx.AsyncClient(base_url=self.base_url, auth=auth, timeout=10.0)
        self._latest_session_id: str | None = None
        self._pending_permission: dict[str, Any] | None = None
        self._last_text: dict[str, str] = {}  # session id -> latest finished assistant text

    async def _run_loop(self) -> None:
        while True:
            try:
                async with self._http.stream("GET", "/api/event", timeout=None) as response:
                    response.raise_for_status()
                    logger.info(f"Connected to OpenCode event stream at {self.base_url}/api/event")
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
                # Reconnect if `opencode serve` is not started yet or dropped the stream
                logger.debug(f"[{self.config.id}] OpenCode stream unavailable ({exc}); retrying in 10s.")
                await asyncio.sleep(10.0)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        ev_type = event.get("type", "")
        data = event.get("data") or {}
        session_id = data.get("sessionID")
        if session_id:
            self._latest_session_id = session_id

        if ev_type == "permission.asked":
            self._pending_permission = data
            what = " ".join([data.get("action", "")] + data.get("resources", [])).strip()
            await self._handle_raw_event(f"OpenCode needs approval: {what or 'a tool call'}", force_decision=True)
        elif ev_type == "session.text.ended" and session_id:
            self._last_text[session_id] = data.get("text", "")
        elif ev_type in ("session.execution.succeeded", "session.execution.failed") and session_id:
            text = self._last_text.pop(session_id, "")
            if text:
                await self._handle_raw_event(text, force_decision=ev_type.endswith("failed"))

    async def send_instruction(self, instruction: str) -> bool:
        try:
            if self._pending_permission is not None:
                request, self._pending_permission = self._pending_permission, None
                resp = await self._http.post(
                    f"/api/session/{request['sessionID']}/permission/{request['id']}/reply",
                    json={"decision": "once" if is_approval(instruction) else "reject"},
                )
                return resp.is_success
            if not self._latest_session_id:
                return False
            resp = await self._http.post(
                f"/api/session/{self._latest_session_id}/prompt",
                json={"text": instruction},
            )
            return resp.is_success
        except httpx.HTTPError as exc:
            logger.error(f"Failed to send message to OpenCode: {exc}")
            return False

    async def stop(self) -> None:
        await super().stop()
        await self._http.aclose()


class AgentOrchestrator:
    """Manages all coding agent watchers and routes user verbal decisions back to the right agent."""

    def __init__(self, state: JarvisState, executive_filter: ExecutiveFilter) -> None:
        self.state = state
        self.executive_filter = executive_filter
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
                self.adapters[cfg.id] = ClaudeCodeStreamAdapter(cfg, self.state, self.executive_filter)
            elif cfg.type == "acp_stdio":
                self.adapters[cfg.id] = ACPStdioAdapter(cfg, self.state, self.executive_filter)
            elif cfg.type == "opencode_sse":
                self.adapters[cfg.id] = OpenCodeSSEAdapter(cfg, self.state, self.executive_filter)

    async def start_all(self) -> None:
        for adapter in self.adapters.values():
            await adapter.start()

    async def stop_all(self) -> None:
        for adapter in self.adapters.values():
            await adapter.stop()

    async def route_user_decision(self, agent_id: str, instruction: str) -> bool:
        adapter = self.adapters.get(agent_id)
        if not adapter:
            return False
        return await adapter.send_instruction(instruction)
