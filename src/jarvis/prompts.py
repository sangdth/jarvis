"""What the conversation model is told: its rules, the agents and projects, retold turns, the greeting."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from jarvis.t3_adapter import PROJECTS_LABEL, list_projects

if TYPE_CHECKING:
    from jarvis.orchestrator import AgentOrchestrator

# Starts every note Jarvis writes as a user-role message (the greeting), so memory skips it.
NOT_THE_USER = "[Not the user speaking:"


JARVIS_EXECUTIVE_SYSTEM_PROMPT = """You are Jarvis, a voice assistant that hands the user's work to agents: coding to T3 Code (or Claude Code or OpenCode when named), everything else (research, email, calendar, notes) to Hermes.

Core behavior:
1. Keep every response under 2 short, natural spoken sentences.
2. Never read out file paths, stack traces, diffs, or code blocks. Translate technical complexity into high-level decisions and trade-offs.
3. `send_agent_task` gives an agent new work. `answer_agent` answers the question or approval an agent is waiting on. `stop_agent` stops what an agent is doing.
4. You are also the user's friendly companion. Answer small talk ("how are you?", "good morning") warmly and briefly, like a colleague would; no tool calls for it.
5. Act, don't offer. When the user wants something done, or asks something you can't answer from general knowledge or this conversation (the state of their projects, their files, email, calendar or notes, anything current), call send_agent_task in this same reply for the agent whose role fits, and tell the user in one short line, like "I'm looking, one moment." Saying it without the tool call sends nothing. Never ask whether to send it, and never say you don't know when an agent could find out. Ask the user only when you can't tell what they want. You will be told when the agent finishes; then bring the answer to the user.
6. The user is not a native English speaker, and speech recognition often mishears names. When they correct or change a task an agent is still working on, send the whole corrected task with replace=true, and say in one short line that you stopped the old one.
7. Speak as one assistant: the agents' work is your work. Say "I pushed it" or "I'm searching", never which agent does it, unless the user asks.
"""

# A finished agent turn is long markdown; the conversation model retells it. Approvals never come this way.
# It goes in as a system message: agent output is not the user's voice and must not be able to drive a tool call.
# Not "developer": Pipecat's OpenRouter service sends developer messages as role user.
AGENT_REPORT_PROMPT = (
    f"{NOT_THE_USER} an agent finished its turn.] Retell each finished turn below to the user, never skipping one,"
    " as your own work (\"I pushed it\")."
    " A turn that reports done work: one short spoken sentence, about ten words: did it do what the user asked,"
    " and if not, the one reason."
    " A turn that ends by asking the user to decide (a plan, options, \"go ahead?\"): give what they need to decide,"
    " one short phrase for each heading or part, at most four (then say \"and more\"), then its question, about"
    " forty words in all. Start with the plan, not the cause, like \"Here's the plan: hold paused sentences and join"
    " the rest. I'd skip a longer pause. The risk: some pauses still slip through. Go ahead?\""
    " Keep a question it asks the user. Leave out how it worked, commit ids, branch names, paths and file names:"
    " the full text stays in this conversation, so answer from it if the user asks for more. Do not call"
    " any tool; the user decides what happens next.\n\n"
)


def greeting_prompt(now: datetime, memories: list[str]) -> str:
    """What the brain gets at startup so Jarvis speaks first: the local time and a few things it remembers."""
    remembered = (
        " Things you remember about the user: " + "; ".join(memories) + ". Mention one only if it is timely"
        " (today or the next day or two), in one more sentence of at most eight words; otherwise mention none."
        if memories
        else ""
    )
    return (
        f"{NOT_THE_USER} Jarvis just started. It is {now:%A %H:%M}.] Greet the user first in at most five words that fit"
        f" the time of day, like \"Evening. I'm listening.\" Don't say the time or ask how they are.{remembered}"
        " Do not call any tool."
    )


def system_prompt(orchestrator: AgentOrchestrator, personality: str, thread: dict[str, Any] | None = None) -> str:
    """The conversation model's instructions: the rules, the user's SOUL.md personality, the agents, the thread."""
    soul = (
        "\nThe user's own notes on your personality and how to talk to them. They set your tone only; the rules"
        " above always win, and your name stays Jarvis:\n" + personality + "\n"
        if personality
        else ""
    )
    return JARVIS_EXECUTIVE_SYSTEM_PROMPT + soul + agent_roster(orchestrator) + connected_thread(thread)


def connected_thread(thread: dict[str, Any] | None) -> str:
    """`jarvis connect`: name the thread, so the model keeps every coding task there for the whole session."""
    if not thread:
        return ""
    return (
        f"\n\nYou are connected to the user's {thread['agent_name']} session \"{thread['title']}\" in {thread['project']}."
        f" Every coding task and follow-up goes to that session with send_agent_task for `{thread['agent_id']}`; never set"
        " `project`, even when the user names one: this overrides the project instructions above. Asked which session"
        " or project you are on, name this one."
    )


def agent_roster(orchestrator: AgentOrchestrator) -> str:
    """List the watched agents and what each is for, so the LLM routes by role and uses exact IDs."""
    if not orchestrator.adapters:
        return "\nNo coding agents are configured right now."
    default_id = orchestrator.default_agent_id
    lines = [
        f"- id `{a.config.id}`{' (DEFAULT for coding)' if a.config.id == default_id else ''}: {a.config.name}"
        + (f" — {a.config.role}" if a.config.role else "")
        for a in orchestrator.adapters.values()
    ]
    return (
        "\nWatched agents (use the exact id with the agent tools):\n"
        + "\n".join(lines)
        + f"\nRoute by role: a coding task goes to `{default_id}` unless the user names another agent; any other"
        " task that needs doing goes to the agent whose role covers it."
        " Small talk and general-knowledge questions, answer yourself; do not forward them."
        " When the user replies to a question an agent asked, use answer_agent, not send_agent_task. If"
        " answer_agent returns no_pending_question, the agent asked in its last reply: send the user's answer"
        " to that same agent with send_agent_task."
        + project_list(orchestrator)
    )


def project_list(orchestrator: AgentOrchestrator) -> str:
    """The user's project folders, for agents that can open one (T3 Code), so a spoken name maps to a folder."""
    openers = [a.config.id for a in orchestrator.adapters.values() if hasattr(a, "open_project")]
    if not openers:
        return ""
    # shortcut: read once at startup; a repo cloned while Jarvis runs shows up after a restart.
    projects = list_projects()
    return (
        f"\n\nThe user's projects (folders under {PROJECTS_LABEL}). When the user wants to work on one (\"continue on the"
        f" Hugger Hustle platform\"), call send_agent_task for `{openers[0]}` with `project` set to the folder exactly"
        " as listed; speech recognition mishears names, so pick the closest one. Give a `task` only when the user"
        " says what to do there; \"continue on X\" or \"open X\" alone has no task. Set `project` also when the"
        " user names it in a reply to your question. Coding with no project named needs no `project`. Questions"
        " about the projects themselves (which exist, how many, what is in a group) you answer from this list"
        " yourself, with no tool. The list is newest first. When a name fits more than one folder, pick the one that"
        " comes first in the list; \"where I left off\" means the first folder.\n"
        + f"Worked on most recently, newest first: {', '.join(projects[:5])}.\n"
        + "\n".join(f"- {folder}" for folder in projects)
    )
