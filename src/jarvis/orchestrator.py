from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from loguru import logger

from jarvis.agent_adapter import BaseAgentAdapter, WatchedAgentConfig
from jarvis.claude_adapter import ClaudeCodeAdapter
from jarvis.config import settings
from jarvis.hermes_adapter import HermesAdapter
from jarvis.opencode_adapter import OpenCodeSSEAdapter
from jarvis.sessions import CodingSessionAdapter
from jarvis.state import JarvisMode, JarvisState
from jarvis.t3_adapter import T3Adapter
from jarvis.waiting_lines import WaitingLines

# agents.json `type` -> its adapter; a new agent is a new <name>_adapter.py and one line here.
ADAPTERS: dict[str, type[BaseAgentAdapter]] = {
    "claude_code": ClaudeCodeAdapter,
    "opencode_sse": OpenCodeSSEAdapter,
    "hermes": HermesAdapter,
    "t3": T3Adapter,
}




class AgentOrchestrator:
    """Manages all coding agent watchers and routes user verbal decisions back to the right agent."""

    def __init__(self, state: JarvisState, personality: str = "") -> None:
        self.state = state
        self.adapters: dict[str, BaseAgentAdapter] = {}
        self._reminders: dict[str, tuple[asyncio.Task, WaitingLines]] = {}
        self._personality = personality  # SOUL.md, the tone of the waiting lines
        # Writes the waiting lines; without a key there is none, and the base lines are used as they are.
        self._http = (
            httpx.AsyncClient(
                base_url=settings.openrouter_base_url,
                timeout=30.0,
                headers={"Authorization": f"Bearer {settings.openrouter_api_key}", "X-Title": "Jarvis"},
            )
            if settings.openrouter_api_key
            else None
        )

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
            adapter_cls = ADAPTERS.get(cfg.type)
            if adapter_cls is None:
                logger.warning(f"Agent {cfg.id!r} has unknown type {cfg.type!r}; skipped.")
                continue
            self.adapters[cfg.id] = adapter_cls(cfg, self.state)

        default = settings.default_coding_agent
        if default and default not in self.adapters:
            raise ValueError(f"DEFAULT_CODING_AGENT={default!r} is not an enabled agent id; use one of {list(self.adapters)}")

    async def start_all(self) -> None:
        connected = next((a for a in self.adapters.values() if a.connected), None)
        for adapter in self.adapters.values():
            # `jarvis connect` sends every coding task to `connected`; skip starting the other coding watchers.
            if connected is not None and isinstance(adapter, (T3Adapter, CodingSessionAdapter)) and adapter is not connected:
                continue
            await adapter.start()

    async def stop_all(self) -> None:
        for adapter in self.adapters.values():
            await adapter.stop()
        if self._http:
            await self._http.aclose()

    def remind_while_busy(self, agent: BaseAgentAdapter, task: str) -> None:
        """After a task is sent: say "Still working on it" while the agent works, so the user knows it isn't forgotten."""
        old = self._reminders.pop(agent.config.id, None)
        if old:  # a new task restarts the count and the lines
            old[0].cancel()
            old[1].stop()
        # Written from the user's task only, never from what an agent wrote
        lines = WaitingLines(task, self._personality, self._http)
        lines.start()
        self._reminders[agent.config.id] = (asyncio.create_task(self._remind(agent, lines)), lines)

    async def _remind(self, agent: BaseAgentAdapter, lines: WaitingLines) -> None:
        wait, next_wait = 8, 13  # Fibonacci seconds: 8, 13, 21, 34, 55, 89, 144, ...
        try:
            while True:
                await asyncio.sleep(wait)
                if not agent.busy:
                    return
                waiting_on_user = agent.config.id in self.state.active_agent_context  # it asked something
                if not waiting_on_user and self.state.mode == JarvisMode.VOICE and self.state.on_speak:
                    await self.state.on_speak(await lines.next())
                wait, next_wait = next_wait, wait + next_wait
        finally:
            lines.stop()  # the agent finished: no batch for a task that's over

    @property
    def t3(self) -> T3Adapter | None:
        """T3 Code, the first-class client: projects and threads of its own. None when it is not enabled."""
        return next((a for a in self.adapters.values() if isinstance(a, T3Adapter)), None)

    @property
    def default_agent_id(self) -> str | None:
        """The agent picked with `jarvis connect`, else DEFAULT_CODING_AGENT, else the first loaded one."""
        connected = next((a.config.id for a in self.adapters.values() if a.connected), None)
        return connected or settings.default_coding_agent or next(iter(self.adapters), None)

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
