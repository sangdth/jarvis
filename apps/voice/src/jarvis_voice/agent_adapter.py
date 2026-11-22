"""What every agent adapter shares: its config, the base class, and how a spoken answer maps onto a question."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from loguru import logger

# One definition for every client: the voice, T3 Code, Claude Code and OpenCode all map answers the same way.
from jarvis_core.questions import describe_fields, form_answer, is_approval, question_fields  # noqa: F401
from jarvis_voice.state import AgentTopic, JarvisState


@dataclass
class WatchedAgentConfig:
    id: str
    name: str
    type: str  # a key of orchestrator.ADAPTERS
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
        self.connected = False  # picked with `jarvis connect`: every task goes to it this run, it never leaves

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

    async def answer(self, answer: str) -> bool | str:
        """Answer the agent's pending question or permission request. False when nothing is pending; a status word
        when the question can't be answered from here."""
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
