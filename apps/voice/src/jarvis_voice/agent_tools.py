"""The agent tools the conversation model can call, and their handlers."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger

from jarvis_voice import meeting_notes
from jarvis_voice.config import settings
from jarvis_voice.orchestrator import AgentOrchestrator
from jarvis_voice.screen import ScreenError, ask_screen, capture_front_window
from jarvis_voice.sessions import CodingSessionAdapter, match_sessions
from jarvis_voice.state import WorkingContext
from jarvis_voice.t3_adapter import resolve_project


def agent_tools() -> list:
    """The tools the conversation model can call (also used by scripts/bench_routing.py).

    The two comment tools are always listed but refuse outside meeting mode (`JarvisState.meeting`): meeting mode can
    be toggled by voice mid-run, and a tool that appears and disappears would leave the model with a stale schema.
    """
    from pipecat.adapters.llm.schemas.tools_schema import FunctionSchema

    agent_id_property = {
        "type": "string",
        "description": (
            "Exact ID of the watched agent, from the list in your instructions. If omitted: a task goes to the"
            " default coding agent, an answer to the agent that asked, a stop to the agent that is working."
        ),
    }
    tools = [
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
                    "description": "Only when the user asks for it: \"new\" to start a clean session (a T3 Code thread"
                    " or a Claude Code, OpenCode or Pi conversation), or the user's words naming an earlier session to"
                    " continue (e.g. 'the speaker verification one'), matched against session titles. Omit otherwise;"
                    " a plain task stays in the current session. With `session`, omit `task` when the user only"
                    " starts or continues a session and names no work.",
                },
                "include_screen": {
                    "type": "boolean",
                    "description": "True only when the task is about what the user points at on their screen right"
                    " now ('fix this error', 'the popup on my screen'): a screenshot of their front window goes with"
                    " it. Omit for ordinary tasks, even about code or a build.",
                },
                "replace": {
                    "type": "boolean",
                    "description": (
                        "True when the user corrects or changes a task this agent is still working on (a misheard"
                        " name, a changed detail, 'no, I meant ...'): the old task is stopped and this one runs"
                        " instead, so give the whole corrected task. False or omitted for an extra, separate task."
                    ),
                },
                "side_question": {
                    "type": "boolean",
                    "description": (
                        "True when working context is being held (`hold_context`) and the user pauses their"
                        " explanation to ask for information or a quick lookup ('what version of React are we using"
                        " right now?') before finishing the main topic: runs this lookup while keeping the held"
                        " context open for when they continue. Omit or false when acting on the main task."
                    ),
                },
            },
            required=[],
        ),
        FunctionSchema(
            name="hold_context",
            description=(
                "Jot down working context while the user is explaining a bigger problem, bug, symptoms, reproduction"
                " steps, or background across multiple turns without asking you to act yet ('let me explain a bug...',"
                " 'when I click save the modal closes...'). Starts no agent work. After calling it, reply with only a"
                " brief acknowledgment ('Mm-hmm.', 'I see.', 'Got it, go on.') so the user can keep speaking. Never"
                " use this for a direct one-shot command or question (call send_agent_task immediately), for saving a"
                " permanent personal note ('save a note that...' -> send_agent_task), or for small talk."
            ),
            properties={
                "note": {
                    "type": "string",
                    "description": (
                        "Concise English note of what the user just explained (symptoms, steps, files, theory),"
                        " translated if they spoke another language."
                    ),
                },
                "project": {
                    "type": "string",
                    "description": "Folder from the user's project list if named in this turn; omit to keep the held one.",
                },
                "clear": {
                    "type": "boolean",
                    "description": "True only when the user drops or cancels the unfinished topic ('never mind that').",
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
        FunctionSchema(
            name="list_sessions",
            description=(
                "List a coding agent's sessions (T3 Code calls them threads; speech recognition often hears"
                " \"sections\"): their titles, project, when last used, which one is running and which one you are"
                " on. Use it when the user asks what sessions exist, how many, or their names. Read the titles out."
                " Not for continuing a session the user describes ('go back to the login one'): that is"
                " send_agent_task with `session`."
            ),
            properties={
                "agent_id": agent_id_property,
                "project": {
                    "type": "string",
                    "description": "A folder from the user's project list, to list only that project's sessions."
                    " Omit for every project.",
                },
            },
            required=[],
        ),
        FunctionSchema(
            name="look_at_screen",
            description=(
                "Look at the user's front window to answer a question about what is on their screen right now"
                " ('what does this popup mean?', 'read me that error'). Returns the answer to speak. For work on"
                " it, use send_agent_task with include_screen instead."
            ),
            properties={
                "question": {"type": "string", "description": "The user's question, in English."},
            },
            required=["question"],
        ),
    ]
    return tools + comment_tools()


def comment_tools() -> list:
    """The meeting-only tools: anyone may leave a note; nothing else happens because of it."""
    from pipecat.adapters.llm.schemas.tools_schema import FunctionSchema

    return [
        FunctionSchema(
            name="add_comment",
            description=(
                "Save a comment someone in the meeting wants kept ('note that…', 'add a comment:…', a decision or a"
                " point worth remembering). It is written to today's meeting notes with who said it; it starts no"
                " work and approves nothing. Then acknowledge in a few words ('Noted.'). Use it for remarks, not for"
                " requests: work goes to send_agent_task."
            ),
            properties={
                "comment": {
                    "type": "string",
                    "description": "The comment as it should read in the notes, in the speaker's language, without"
                    " 'Jarvis' or 'note that'.",
                },
            },
            required=["comment"],
        ),
        FunctionSchema(
            name="read_comments",
            description=(
                "Read today's meeting comments back. Use it when someone asks what was noted, what the decision was,"
                " or for a summary of the meeting so far. Summarize them; do not read every line."
            ),
            properties={},
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
            # A guest may ask for work, never approve, stop, replace, switch sessions/projects, nor see the master's screen.
            if guest and (
                owner_only or args.get("replace") or args.get("include_screen") or args.get("session") or args.get("project")
            ):
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
            ok = await action(agent, args)  # True, False, a status of its own, or a result with its status
            if isinstance(ok, dict):
                await params.result_callback({"agent_id": agent.config.id, **ok})
                return
            status = ok if isinstance(ok, str) else ok_status if ok else fail_status
            await params.result_callback({"status": status, "agent_id": agent.config.id})

        return handle

    async def send(agent: Any, args: dict) -> bool | str | dict:
        if args.get("replace") and agent.busy:
            await agent.interrupt()  # a correction: the old task must not come back as a second answer
        ctx = orchestrator.state.working_context
        has_held = isinstance(ctx, WorkingContext) and orchestrator.state.room_turn is not True and ctx.active()
        side = bool(args.get("side_question"))
        task, project = args.get("task", "").strip(), args.get("project")
        session = (args.get("session") or "").strip()
        if has_held and not side and task and not project and not session and ctx.project:
            project = ctx.project
        moved: dict = {}
        if isinstance(agent, CodingSessionAdapter) and (session or project) and not agent.connected:
            moved = await switch_session(agent, session, project)
            if moved["status"] != "session_ready" or not task:
                return moved  # nothing went out: a refusal, or "continue the login session" alone
        if not task:
            return "nothing_to_send"  # "continue on X" while connected: not a failure
        held_prefix = ctx.format_for_task() if has_held and not side else ""
        prompt = f"{held_prefix}\n\n{task}" if held_prefix else task
        if args.get("include_screen"):
            try:
                shot = await capture_front_window()
            except ScreenError as exc:
                return exc.status  # nothing went out: the task needs the screen it points at
            # Only the agent gets this line: the waiting lines below are written from `task` by a model and spoken.
            prompt += f"\n\n[Screenshot of the user's front window ({shot.app}: {shot.title}): {shot.path}]"
        before = agent.session_id if isinstance(agent, CodingSessionAdapter) else None
        from jarvis_voice.brain_client import BrainClientAdapter

        if isinstance(agent, BrainClientAdapter):
            sent = await agent.send_task(prompt, replace=bool(args.get("replace")))
        else:
            sent = await agent.send_task(prompt)
        if not sent:
            return False
        if has_held and not side:
            ctx.clear()
        orchestrator.remind_while_busy(agent, task)
        t3 = orchestrator.t3
        if agent is t3 and t3 is not None and t3.session_id != before:  # T3 continued its newest thread, or started one
            moved = {**moved, "session": t3.title or "new"}
        if has_held and side:
            moved = {**moved, "held_notes": len(ctx.notes)}
        return {**moved, "status": "sent"}

    async def switch_session(agent: CodingSessionAdapter, session: str, project: str | None) -> dict:
        """Point a coding client at the session the user named, a new one, or its newest one in a named project."""

        def refused() -> dict:  # pick and new say only False: mid-turn, or the client can't (Pi opens no window)
            return {"status": "busy" if agent.busy else "not_switched"}

        cwd = None
        if project:
            folder = resolve_project(project)  # only a listed folder
            if folder is None:
                return {"status": "unknown_project"}
            cwd = str(folder)
        if session.lower() == "new":
            if not await agent.new_session(cwd):
                return refused()
            return {"status": "session_ready", "session": "new", "project": Path(cwd or agent.cwd).name}
        if not session and getattr(agent, "_fresh", False):
            # A staged "new session" survives the next turn naming a project, instead of picking an old thread.
            if cwd and cwd != agent.cwd and not await agent.new_session(cwd):
                return refused()
            return {"status": "session_ready", "project": Path(cwd or agent.cwd).name}
        if not session and cwd == agent.cwd and (agent.session_id or getattr(agent, "_fresh", False)):
            return {"status": "session_ready"}  # already working there
        try:
            found = await agent.list_sessions(cwd or (None if session else agent.cwd), limit=50)
        except Exception as exc:
            logger.warning(f"[{agent.config.id}] Could not list sessions ({exc!r}).")
            return {"status": "sessions_unavailable"}
        if session:
            hits = match_sessions(found, session)
            if not hits:
                return {"status": "no_matching_session", "sessions": [s.title for s in found[:8]]}
        else:  # a project alone: its newest session, never one running now (it may be the user's own, typed by hand)
            hits = [s for s in found if not s.running]
            if not hits:
                if not await agent.new_session(cwd):  # a project with no session yet starts one
                    return refused()
                return {"status": "session_ready", "session": "new", "project": Path(cwd or agent.cwd).name}
        if not await agent.pick_session(hits[0]):
            return refused()
        return {"status": "session_ready", "session": hits[0].title, "project": Path(hits[0].cwd).name}

    async def sessions(params: Any) -> None:
        args = params.arguments
        if orchestrator.state.room_turn is True:  # a meeting guest never hears the master's session titles
            await params.result_callback({"status": "owner_only"})
            return
        agent = orchestrator.find(args.get("agent_id"))
        if not isinstance(agent, CodingSessionAdapter):
            await params.result_callback({"status": "no_sessions", "agent_id": args.get("agent_id")})
            return
        cwd = None
        if args.get("project"):
            folder = resolve_project(args["project"])
            if folder is None:
                await params.result_callback({"status": "unknown_project", "agent_id": agent.config.id})
                return
            cwd = str(folder)
        try:
            found = await agent.list_sessions(cwd, limit=10)
        except Exception as exc:
            logger.warning(f"[{agent.config.id}] Could not list sessions ({exc!r}).")
            await params.result_callback({"status": "sessions_unavailable", "agent_id": agent.config.id})
            return
        await params.result_callback({
            "status": "listed", "agent_id": agent.config.id, "count": len(found),
            "sessions": [
                {"title": s.title, "project": Path(s.cwd).name,
                 "updated": time.strftime("%b %d %H:%M", time.localtime(s.updated_at)) if s.updated_at > 0 else "",
                 **({"running": True} if s.running else {}), **({"current": True} if s.id == agent.session_id else {})}
                for s in found
            ],
        })

    async def look(params: Any) -> None:
        if orchestrator.state.room_turn is True:  # a meeting guest never sees the master's screen
            await params.result_callback({"status": "owner_only"})
            return
        if orchestrator._http is None:
            await params.result_callback({"status": "no_model"})
            return
        try:
            shot = await capture_front_window()
            answer = await ask_screen(
                shot, params.arguments.get("question", "What is on the screen?"), orchestrator._http
            )
        except ScreenError as exc:
            await params.result_callback({"status": exc.status})
            return
        await params.result_callback({"status": "seen", "answer": answer})

    async def add_comment(params: Any) -> None:
        """A meeting note: anyone may add one, and it only ever reaches the day's notes file."""
        if not orchestrator.state.meeting:
            await params.result_callback({"status": "not_in_meeting"})
            return
        try:
            path, count = meeting_notes.append_comment(
                str(params.arguments.get("comment") or ""),
                orchestrator.state.current_speaker,
            )
        except ValueError:
            await params.result_callback({"status": "nothing_to_note"})
            return
        logger.info(f"[Meeting] comment saved to {path} ({count} today)")
        await params.result_callback({"status": "comment_saved", "count": count})

    async def read_comments(params: Any) -> None:
        if not orchestrator.state.meeting:
            await params.result_callback({"status": "not_in_meeting"})
            return
        notes = meeting_notes.read_comments()
        await params.result_callback({"status": "read", "comments": notes} if notes else {"status": "no_comments"})

    async def hold(params: Any) -> None:
        if orchestrator.state.room_turn is True:  # a meeting guest cannot alter the owner's working scratchpad
            await params.result_callback({"status": "owner_only"})
            return
        ctx = orchestrator.state.working_context
        if not isinstance(ctx, WorkingContext):
            ctx = WorkingContext()
            orchestrator.state.working_context = ctx
        args = params.arguments
        if args.get("clear"):
            ctx.clear()
            await params.result_callback({"status": "cleared"})
            return
        note = str(args.get("note") or "").strip()
        raw_project = str(args.get("project") or "").strip()
        resolved = raw_project if raw_project and resolve_project(raw_project) is not None else None
        if raw_project and resolved is None and not note:
            await params.result_callback({"status": "unknown_project"})
            return
        if not note and not resolved:
            await params.result_callback({"status": "nothing_to_hold"})
            return
        ctx.add(note=note, project=resolved)
        await params.result_callback({
            "status": "held",
            "notes": list(ctx.notes),
            **({"project": ctx.project} if ctx.project else {}),
        })

    async def stop(agent: Any, args: dict) -> bool | str:
        # Nothing running: say so, instead of a "failed" the model would read out as "I couldn't stop it".
        return await agent.interrupt() if agent.busy else "nothing_running"

    handlers: dict[str, Callable[[Any], Awaitable[None]]] = {
        "send_agent_task": handler(send, "sent", "failed", guest_waits=True),
        "hold_context": hold,
        # No id: an answer goes to the agent that asked, a stop to the one that is working.
        "answer_agent": handler(
            lambda a, args: a.answer(args.get("answer", "")),
            "answered",
            "no_pending_question",
            orchestrator.find_waiting,
            owner_only=True,
        ),
        "stop_agent": handler(stop, "stopped", "failed", orchestrator.find_busy, owner_only=True),
        "list_sessions": sessions,
        "look_at_screen": look,
        # Both are open to the whole room; they refuse themselves outside meeting mode.
        "add_comment": add_comment,
        "read_comments": read_comments,
    }
    return handlers
