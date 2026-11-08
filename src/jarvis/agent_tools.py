"""The agent tools the conversation model can call, and their handlers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from jarvis.config import settings
from jarvis.orchestrator import AgentOrchestrator
from jarvis.sessions import CodingSessionAdapter, match_sessions
from jarvis.t3_adapter import resolve_project


def agent_tools() -> list:
    """The three agent tools the conversation model can call (also used by scripts/bench_routing.py)."""
    from pipecat.adapters.llm.schemas.tools_schema import FunctionSchema

    agent_id_property = {
        "type": "string",
        "description": (
            "Exact ID of the watched agent, from the list in your instructions. If omitted: a task goes to the"
            " default coding agent, an answer to the agent that asked, a stop to the agent that is working."
        ),
    }
    return [
        FunctionSchema(
            name="send_agent_task",
            description=(
                "Give a watched agent new work: a coding task, or any other task for the agent whose role covers it, "
                "or a follow-up to its last reply."
            ),
            properties={
                "agent_id": agent_id_property,
                "task": {
                    "type": "string",
                    "description": "The task, in clear English words, even when the user speaks another language. With"
                    " `project`, omit it when the user only opens the project and names no work; never invent one.",
                },
                "project": {
                    "type": "string",
                    "description": "A folder from the user's project list (e.g. huggerhustle/platform-v2) when the"
                    " user names a project to work on. Omit otherwise.",
                },
                "session": {
                    "type": "string",
                    "description": "Claude Code, OpenCode or Pi only, when the user asks for it: \"new\" to start a"
                    " clean conversation, or the user's words naming an earlier session of that agent to continue"
                    " (e.g. 'the speaker verification one'). Omit otherwise; a plain task stays in the current session.",
                },
                "replace": {
                    "type": "boolean",
                    "description": (
                        "True when the user corrects or changes a task this agent is still working on (a misheard"
                        " name, a changed detail, 'no, I meant ...'): the old task is stopped and this one runs"
                        " instead, so give the whole corrected task. False or omitted for an extra, separate task."
                    ),
                },
            },
            required=[],
        ),
        FunctionSchema(
            name="answer_agent",
            description=(
                "Answer the question or approval an agent is waiting on. "
                "Returns no_pending_question if it asked nothing."
            ),
            properties={
                "agent_id": agent_id_property,
                "answer": {
                    "type": "string",
                    "description": "The user's answer in English, translated if they spoke another language"
                    " (e.g. 'yes', 'use Postgres')",
                },
            },
            required=["answer"],
        ),
        FunctionSchema(
            name="stop_agent",
            description="Stop what an agent is doing right now.",
            properties={"agent_id": agent_id_property},
            required=[],
        ),
    ]


def agent_tool_handlers(orchestrator: AgentOrchestrator) -> dict[str, Callable[[Any], Awaitable[None]]]:
    """Handlers for agent_tools(). Pipecat 1.x calls each with one FunctionCallParams (arguments, result_callback)."""

    def handler(
        action, ok_status: str, fail_status: str, find=orchestrator.find, owner_only: bool = False, guest_waits: bool = False
    ):
        async def handle(params: Any) -> None:
            args = params.arguments
            guest = orchestrator.state.room_turn is True  # `jarvis meeting`, master check on, not the master's voice
            # A guest may ask for work, never approve, stop or replace it.
            if guest and (owner_only or args.get("replace")):
                await params.result_callback({"status": "owner_only", "agent_id": args.get("agent_id")})
                return
            # MEETING_GUESTS=ask: nothing runs until the master says yes; the model then sends it again on that turn.
            if guest and guest_waits and settings.meeting_guests == "ask":
                await params.result_callback(
                    {"status": "needs_owner_ok", "agent_id": args.get("agent_id"), "task": args.get("task")}
                )
                return
            agent = find(args.get("agent_id"))
            if agent is None:
                await params.result_callback({"status": "agent_not_active", "agent_id": args.get("agent_id")})
                return
            ok = await action(agent, args)  # True, False, or a status of its own
            status = ok if isinstance(ok, str) else ok_status if ok else fail_status
            await params.result_callback({"status": status, "agent_id": agent.config.id})

        return handle

    async def send(agent: Any, args: dict) -> bool | str:
        if args.get("replace") and agent.busy:
            await agent.interrupt()  # a correction: the old task must not come back as a second answer
        task, project = args.get("task", "").strip(), args.get("project")
        session = (args.get("session") or "").strip()
        if isinstance(agent, CodingSessionAdapter) and (session or project) and not agent.connected:
            moved = await switch_session(agent, session, project)
            if moved is not True:
                return moved  # a status: nothing went out
            if not task:
                return "session_ready"  # "continue the login session" alone: ready for the next words
        # T3 Code opens a project in a thread of its own; while connected, a project is no reason to leave the session.
        opens = bool(project) and agent is orchestrator.t3
        if not task and not opens:
            return "nothing_to_send"  # "continue on X" while connected: not a failure
        ok = await (agent.open_project(project, task) if opens else agent.send_task(task))
        if ok is True:  # a status word means nothing went out, so nothing to wait on
            orchestrator.remind_while_busy(agent, task or f"open the {project} project")
        return ok

    async def switch_session(agent: CodingSessionAdapter, session: str, project: str | None) -> bool | str:
        """Point a coding client at the session the user named, a new one, or its newest one in a named project."""

        def refused() -> str:  # pick and new say only False: mid-turn, or the client can't (Pi opens no window)
            return "busy" if agent.busy else "not_switched"

        cwd = None
        if project:
            folder = resolve_project(project)  # only a listed folder, as for T3 Code
            if folder is None:
                return "unknown_project"
            cwd = str(folder)
        if session.lower() == "new":
            return await agent.new_session(cwd) or refused()
        if not session and cwd == agent.cwd:
            return True  # already working there
        try:
            found = await agent.list_sessions(cwd, limit=50)
        except Exception as exc:
            logger.warning(f"[{agent.config.id}] Could not list sessions ({exc!r}).")
            return "sessions_unavailable"
        hits = match_sessions(found, session) if session else found  # a project alone: its newest session
        if not hits:
            if session:
                return "no_matching_session"
            return await agent.new_session(cwd) or refused()  # a project with no session yet starts one
        return await agent.pick_session(hits[0]) or refused()

    async def stop(agent: Any, args: dict) -> bool | str:
        # Nothing running: say so, instead of a "failed" the model would read out as "I couldn't stop it".
        return await agent.interrupt() if agent.busy else "nothing_running"

    return {
        "send_agent_task": handler(send, "sent", "failed", guest_waits=True),
        # No id: an answer goes to the agent that asked, a stop to the one that is working.
        "answer_agent": handler(
            lambda a, args: a.answer(args.get("answer", "")),
            "answered",
            "no_pending_question",
            orchestrator.find_waiting,
            owner_only=True,
        ),
        "stop_agent": handler(stop, "stopped", "failed", orchestrator.find_busy, owner_only=True),
    }
