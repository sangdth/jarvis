from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLINotFoundError,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    ToolPermissionContext,
    list_sessions,
)
from loguru import logger

from jarvis.agent_adapter import WatchedAgentConfig, describe_fields, form_answer, is_approval
from jarvis.sessions import AgentSession, CodingSessionAdapter
from jarvis.state import JarvisState


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



class ClaudeCodeAdapter(CodingSessionAdapter):
    """
    Runs Claude Code through the Agent SDK: one long-lived process per session, resumed by id after a crash or a pick.
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
        self._stops = 0  # counts stops, so a question queued before a stop is not asked after it
        self._session_id: str | None = None  # the conversation tasks go to; None until Claude Code names one
        self._cwd = str(Path(config.cwd).expanduser().resolve())

    async def _run_loop(self) -> None:
        while True:
            options = ClaudeAgentOptions(
                cwd=self._cwd,
                resume=self._session_id,  # after a crash or a pick: the same conversation, not an empty one
                # No permission_mode: the user's own settings decide, the same as their Claude Code anywhere else.
                can_use_tool=self._can_use_tool,
                setting_sources=["user", "project", "local"],
                stderr=lambda line: logger.debug(f"[{self.config.id}] {line}"),
            )
            try:
                async with ClaudeSDKClient(options) as client:
                    self._client = client
                    logger.info(f"Started Claude Code [{self.config.id}] in {self._cwd} (session {self._session_id})")
                    async for message in client.receive_messages():
                        await self._handle_message(message)
            except CLINotFoundError:
                logger.warning(f"[{self.config.id}] `claude` CLI not found on PATH; watcher idle.")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[{self.config.id}] Claude Code stopped ({exc!r}); restarting in 10s.")
            finally:
                self._client = None
            # The turn in flight died with the process: no result will come, so it is not running any more.
            if self._running:
                await self._surface("stopped in the middle of a task; restarting it in the same conversation.")
            self._running = self._stopped = False
            await asyncio.sleep(10.0)

    async def _handle_message(self, message: Any) -> None:
        if isinstance(message, SystemMessage) and message.subtype == "init":
            # The CLI names the conversation before the first turn runs, so even a crash in that turn resumes it.
            self._session_id = message.data.get("session_id") or self._session_id
        if isinstance(message, ResultMessage):
            if self._stopped:  # the stopped turn's result; a task sent since then is still running
                self._stopped = False
                return
            self._running = False
            text = message.result or ("" if not message.is_error else f"The turn failed ({message.subtype}).")
            if text:
                await self._surface(text, is_report=True)

    async def _ask(self, spoken: str) -> str | None:
        stops = self._stops
        async with self._ask_lock:
            if stops != self._stops:  # the user stopped the turn while this question waited its turn
                return None
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
        self._stops += 1
        self._stopped, self._running = self._running, False
        await self._client.interrupt()
        return True

    async def list_sessions(self, cwd: str | None = None, limit: int = 15) -> list[AgentSession]:
        found = await asyncio.to_thread(list_sessions, directory=cwd, limit=limit)  # reads ~/.claude/projects
        return [
            AgentSession(
                id=s.session_id,
                title=s.custom_title or s.summary or s.first_prompt or "(untitled)",
                cwd=s.cwd or cwd or self._cwd,
                updated_at=s.last_modified / 1000,
                branch=s.git_branch or "",
            )
            for s in found
        ]

    async def pick_session(self, session: AgentSession, connected: bool = False) -> bool:
        if self.busy:  # restarting the process would drop the running turn without a word
            return False
        self._session_id, self._cwd, self.connected = session.id, session.cwd, connected
        await self._restart()
        return True

    async def new_session(self, cwd: str | None = None) -> bool:
        if self.busy:
            return False
        self._session_id, self._cwd = None, cwd or self._cwd
        await self._restart()
        return True
