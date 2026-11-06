"""The agent tools the conversation model can call, and their handlers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from jarvis.orchestrator import AgentOrchestrator


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
                    "description": "The task, in clear words. With `project`, omit it when the user only opens the"
                    " project and names no work; never invent one.",
                },
                "project": {
                    "type": "string",
                    "description": "A folder from the user's project list (e.g. huggerhustle/platform-v2) when the"
                    " user names a project to work on. Omit otherwise.",
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
                "answer": {"type": "string", "description": "The user's answer, in their words (e.g. 'yes', 'use Postgres')"},
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

    def handler(action, ok_status: str, fail_status: str, find=orchestrator.find):
        async def handle(params: Any) -> None:
            args = params.arguments
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
        # Only T3 Code opens a project; the others work in their configured folder, so a project alone is no task.
        opens = bool(project) and hasattr(agent, "open_project")
        if not task and not opens:
            return "nothing_to_send"  # "continue on X" while connected: not a failure
        ok = await (agent.open_project(project, task) if opens else agent.send_task(task))
        if ok is True:  # a status word means nothing went out, so nothing to wait on
            orchestrator.remind_while_busy(agent)
        return ok

    async def stop(agent: Any, args: dict) -> bool | str:
        # Nothing running: say so, instead of a "failed" the model would read out as "I couldn't stop it".
        return await agent.interrupt() if agent.busy else "nothing_running"

    return {
        "send_agent_task": handler(send, "sent", "failed"),
        # No id: an answer goes to the agent that asked, a stop to the one that is working.
        "answer_agent": handler(
            lambda a, args: a.answer(args.get("answer", "")), "answered", "no_pending_question", orchestrator.find_waiting
        ),
        "stop_agent": handler(stop, "stopped", "failed", orchestrator.find_busy),
    }
