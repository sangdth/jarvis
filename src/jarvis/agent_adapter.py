"""What every agent adapter shares: its config, the base class, and how a spoken answer maps onto a question."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from loguru import logger

from jarvis.state import AgentTopic, JarvisState

# Refusals are checked first, so "not sure" or "don't allow" never count as approval.
_REJECT_WORDS = re.compile(r"\b(no|not|don'?t|do not|never|reject|deny|denied|cancel|stop|wait)\b", re.IGNORECASE)
_APPROVE_WORDS = re.compile(r"\b(yes|yep|yeah|approve|approved|allow|go ahead|sure|do it|ok|okay|proceed)\b", re.IGNORECASE)


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


def is_approval(instruction: str) -> bool:
    """True only when the user clearly approves and says nothing that sounds like a refusal."""
    if _REJECT_WORDS.search(instruction):
        return False
    return bool(_APPROVE_WORDS.search(instruction))


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
