from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path

from loguru import logger

from jarvis.agent_adapter import BaseAgentAdapter, WatchedAgentConfig
from jarvis.claude_adapter import ClaudeCodeAdapter
from jarvis.config import settings
from jarvis.hermes_adapter import HermesAdapter
from jarvis.opencode_adapter import OpenCodeSSEAdapter
from jarvis.state import JarvisMode, JarvisState
from jarvis.t3_adapter import T3Adapter

# agents.json `type` -> its adapter; a new agent is a new <name>_adapter.py and one line here.
ADAPTERS: dict[str, type[BaseAgentAdapter]] = {
    "claude_code": ClaudeCodeAdapter,
    "opencode_sse": OpenCodeSSEAdapter,
    "hermes": HermesAdapter,
    "t3": T3Adapter,
}


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
            adapter_cls = ADAPTERS.get(cfg.type)
            if adapter_cls is None:
                logger.warning(f"Agent {cfg.id!r} has unknown type {cfg.type!r}; skipped.")
                continue
            self.adapters[cfg.id] = adapter_cls(cfg, self.state)

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
