from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import httpx
from loguru import logger

from jarvis.executive_filter import ExecutiveFilter
from jarvis.state import JarvisState


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
    Surfaces only permission requests, questions, or final turn summaries through the ExecutiveFilter.
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
        ]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.config.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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

            event_type = event.get("type", "")
            # Surface assistant completion results or permission prompts
            if event_type == "result":
                result_text = event.get("result", "")
                is_error = bool(event.get("is_error", False))
                await self._handle_raw_event(result_text, force_decision=is_error)
            elif event_type == "assistant":
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        if "?" in text:
                            await self._handle_raw_event(text, force_decision=True)

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
    Watches and orchestrates ACP-compatible agents (Pi, OpenCode ACP, Gemini CLI, etc.)
    over JSON-RPC 2.0 stdio (`pi --acp` or `opencode acp`).
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
        self._pending_permission_id: int | str | None = None

    async def _run_loop(self) -> None:
        cmd = self.config.command or ["pi", "--acp"]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.config.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info(f"Started ACP stdio watcher [{self.config.id}]: {' '.join(cmd)}")
        except FileNotFoundError:
            logger.warning(f"[{self.config.id}] Command {cmd[0]!r} not found on PATH; watcher idle.")
            return

        assert self._proc.stdout is not None
        text_accumulator: list[str] = []

        async for raw_line in self._proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue

            method = msg.get("method", "")
            params = msg.get("params", {})

            # ACP permission request -> always surface immediately to Jarvis
            if method == "session/request_permission":
                self._pending_permission_id = msg.get("id")
                title = params.get("permission", {}).get("title", "Tool execution permission")
                desc = params.get("permission", {}).get("description", "")
                await self._handle_raw_event(
                    f"Permission requested: {title}. {desc}",
                    force_decision=True,
                )

            # ACP streaming session updates
            elif method == "session/update":
                update = params.get("update", {})
                session_update = update.get("sessionUpdate", "")
                if session_update == "agent_message_chunk":
                    chunk = update.get("content", {}).get("text", "")
                    text_accumulator.append(chunk)
                elif session_update in ("turn_complete", "idle") and text_accumulator:
                    full_turn = "".join(text_accumulator)
                    text_accumulator.clear()
                    await self._handle_raw_event(full_turn, force_decision=False)

    async def send_instruction(self, instruction: str) -> bool:
        if not self._proc or not self._proc.stdin:
            return False

        # If waiting on a permission request, resolve it if user said approve/reject
        if self._pending_permission_id is not None:
            approved = any(
                w in instruction.lower()
                for w in ("yes", "approve", "go ahead", "allow", "sure", "do it")
            )
            reply = {
                "jsonrpc": "2.0",
                "id": self._pending_permission_id,
                "result": {"outcome": {"outcome": "selected" if approved else "cancelled"}},
            }
            self._pending_permission_id = None
            self._proc.stdin.write((json.dumps(reply) + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
            return True

        self._rpc_id += 1
        rpc = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": "session/prompt",
            "params": {"prompt": [{"type": "text", "text": instruction}]},
        }
        self._proc.stdin.write((json.dumps(rpc) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()
        return True


class OpenCodeSSEAdapter(BaseAgentAdapter):
    """
    Subscribes to an OpenCode Server (`opencode serve`) over HTTP Server-Sent Events (`/event`).
    """

    def __init__(
        self,
        config: WatchedAgentConfig,
        state: JarvisState,
        executive_filter: ExecutiveFilter,
    ) -> None:
        super().__init__(config, state, executive_filter)
        self.base_url = (config.url or "http://127.0.0.1:4096").rstrip("/")
        self._latest_session_id: str | None = None

    async def _run_loop(self) -> None:
        url = f"{self.base_url}/event"
        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", url) as response:
                        logger.info(f"Connected to OpenCode SSE stream at {url}")
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            raw_data = line.removeprefix("data:").strip()
                            if not raw_data:
                                continue
                            try:
                                event = json.loads(raw_data)
                            except json.JSONDecodeError:
                                continue

                            ev_type = event.get("type", "")
                            props = event.get("properties", {})
                            session_id = props.get("sessionID")
                            if session_id:
                                self._latest_session_id = session_id

                            if ev_type == "permission.updated":
                                title = props.get("title", "Approval needed")
                                await self._handle_raw_event(
                                    f"OpenCode needs approval: {title}",
                                    force_decision=True,
                                )
                            elif ev_type == "session.idle":
                                summary_text = props.get("summary", "")
                                if summary_text:
                                    await self._handle_raw_event(summary_text, force_decision=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Reconnect quietly if `opencode serve` is not started yet
                await asyncio.sleep(10.0)

    async def send_instruction(self, instruction: str) -> bool:
        if not self._latest_session_id:
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self.base_url}/session/{self._latest_session_id}/message",
                    json={"parts": [{"type": "text", "text": instruction}]},
                )
                return resp.is_success
        except Exception as exc:
            logger.error(f"Failed to send message to OpenCode: {exc}")
            return False


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
                self.adapters[cfg.id] = ClaudeCodeStreamAdapter(
                    cfg, self.state, self.executive_filter
                )
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
