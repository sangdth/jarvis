"""Benchmark where Jarvis sends a request: runs the real system prompt, roster and tools on EXECUTIVE_MODEL.

Usage: uv run python scripts/bench_routing.py [rounds]   (needs OPENROUTER_API_KEY; ~240 paid calls at 3 rounds)
Exits 1 when accuracy drops below MIN_ACCURACY.
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import sys
import time

import httpx

from jarvis_core.projects import list_projects, projects_dir
from jarvis_core.soul import load_soul
from jarvis_voice.agent_tools import agent_tools
from jarvis_voice.config import settings
from jarvis_voice.orchestrator import AgentOrchestrator
from jarvis_voice.prompts import system_prompt
from jarvis_voice.state import JarvisState

MIN_ACCURACY = 0.95  # 91% on 2026-11-05 (DeepSeek V4 Pro, thinking on): below the bar

SELF = "answer yourself"  # no tool call


def after(first: str, agent_id: str, task: str, then: str) -> list[dict]:
    """A second sentence said while `agent_id` still works on `task` (sent for the first sentence).

    The roster has one agent now, so the fabricated earlier call uses `brain` whatever label the case came
    with; otherwise the model would copy a stale id out of its own history.
    """
    call = {"id": "call_1", "type": "function",
            "function": {"name": "send_agent_task", "arguments": json.dumps({"agent_id": "brain", "task": task})}}
    return [
        {"role": "user", "content": first},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_1", "content": json.dumps({"status": "sent", "agent_id": "brain"})},
        {"role": "assistant", "content": "On it, one moment."},
        {"role": "user", "content": then},
    ]


def waiting(spoken: str, reply: str) -> list[dict]:
    """The user's reply right after Jarvis spoke an agent's question or approval request."""
    return [{"role": "assistant", "content": spoken}, {"role": "user", "content": reply}]


def explaining(first: str, note: str, then: str, *, project: str = "", middle: list[dict] | None = None) -> list[dict]:
    """A follow-up turn while `hold_context` holds earlier explanation notes."""
    from jarvis_voice.state import WorkingContext

    ctx = WorkingContext()
    ctx.add(note, project or None)
    args = {"note": note, **({"project": project} if project else {})}
    call = {"id": "hold_1", "type": "function", "function": {"name": "hold_context", "arguments": json.dumps(args)}}
    result = {"status": "held", "notes": [note], **({"project": project} if project else {})}
    return [
        {"role": "user", "content": first},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "hold_1", "content": json.dumps(result)},
        {"role": "assistant", "content": "Got it, go on."},
        *(middle or []),
        {"role": "system", "content": ctx.format_for_prompt()},
        {"role": "user", "content": then},
    ]


# Agents only read English: a Vietnamese letter in `task` or `answer` is a miss.
VIETNAMESE = re.compile(r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", re.IGNORECASE)

_projects = list_projects()
_latest_project = _projects[0].lower() if _projects else "jarvis"
_latest_hugger = next((p.lower() for p in _projects if p.lower().startswith("huggerhustle/")), "huggerhustle/platform-v2")

CASES: list[tuple[str | list[dict], str]] = [
    ("Build a login page with email and password", "t3-code"),
    ("Fix the bug in the navbar", "t3-code"),
    ("Add unit tests for the payment module", "t3-code"),
    ("Refactor the auth service to use JWT", "t3-code"),
    ("Why is the build failing?", "t3-code"),
    ("Rename the user table column to email_address", "t3-code"),
    ("Write a Python script that resizes all images in a folder", "t3-code"),
    ("Have OpenCode add dark mode", "opencode"),
    ("Ask OpenCode to update the README", "opencode"),
    ("Tell Claude to run the tests", "claude"),
    ("Ask T3 Code to fix the navbar", "t3-code"),
    ("Have T3 add a dark mode toggle", "t3-code"),
    ("Have Claude Code add a dark mode toggle", "claude"),
    # Sessions of Claude Code and OpenCode: a new one, or an earlier one named by its topic; a plain task sets none
    ("Start a new Claude Code session", "claude+new"),
    ("Open a fresh OpenCode session and add a health check endpoint", "opencode+task+new"),
    ("On Claude Code, continue the session about speaker verification", "claude+session"),
    ("Go back to the OpenCode session where we fixed the login bug", "opencode+session"),
    ("Ask Claude to keep going on the current task", "claude"),
    # Sessions are listed by tool, with titles, never guessed from the screen ("sections" is how Whisper hears it)
    ("How many sections we have inside the T3 code right now?", "list"),
    ("What sessions do I have open?", "list"),
    ("Tell me the names of my T3 threads", "list"),
    ("Which sessions are there in the Jarvis project?", "list"),
    ("Go back to the T3 thread about the meeting bridge", "t3-code+session"),
    ("Start a new session and fix the failing tests", "t3-code+task+new"),
    # A named project opens it in T3 Code; the folder comes from the project list (names are often misheard)
    ("Continue working on the Hugger Hustle platform", "t3-code@huggerhustle/platform-v2"),
    ("Let's work on the Hacker Hustle platform v2 and fix the signup form", "t3-code@huggerhustle/platform-v2+task"),
    ("Open a new session in the Hugger Hustle platform project", "t3-code@huggerhustle/platform-v2+new"),
    ("Open the OpsCom status project", "t3-code@opscom/status"),
    ("I want to work on Jarvis today", "t3-code@jarvis"),
    ("Continue to work with hugger hustle platform", "t3-code@huggerhustle/platform-v2"),
    # A vague or shared name picks the project worked on most recently (live: depends on the machine's repos)
    ("Continue where I left off", f"t3-code@{_latest_project}"),
    ("Continue on Hugger Hustle", f"t3-code@{_latest_hugger}"),
    ("Let's work on the Hugger Hustle blog", "t3-code@huggerhustle/simple-blog"),
    # Questions about the project list itself are answered from the list in the prompt, not sent to an agent
    ("How many projects do we have in Happy Voxel?", SELF),
    ("How many projects do we have in OpsCom?", SELF),
    ("What projects are in Hugger Hustle?", SELF),
    # The project named in a reply to Jarvis's question still opens it (misheard, 2026-11-03)
    ([{"role": "user", "content": "Open a new session on..."},
      {"role": "assistant", "content": "Which project should the new session go in?"},
      {"role": "user", "content": "the hacker hustle platform"}], "t3-code@huggerhustle/platform-v2+new"),
    ("What's on my calendar tomorrow?", "hermes"),
    ("Email Minh that I'll be ten minutes late", "hermes"),
    ("Find me the best rated ramen place nearby", "hermes"),
    ("Remind me to call the bank at 3pm", "hermes"),
    ("Look up the latest news about the Fed rate decision", "hermes"),
    ("Save a note that the wifi password for the office changed", "hermes"),
    ("Research the pros and cons of solar panels for my house", "hermes"),
    ("Check the weather in Helsinki this weekend", "hermes"),
    ("Book a meeting with Anna on Friday afternoon", "hermes"),
    ("Summarize my unread emails", "hermes"),
    ("Tell Hermes to find a cheap flight to Hanoi in December", "hermes"),
    # Things Jarvis can't know must be sent, not offered ("Want me to send it over?")
    ("What is the status of the OpsCom demo project?", "hermes"),
    ("Did Anna reply to my email yet?", "hermes"),
    ("What's the Bitcoin price right now?", "hermes"),
    ("How are you?", SELF),
    ("Good morning", SELF),
    ("Tell me a joke", SELF),
    ("Thanks, that's great", SELF),
    ("What's the difference between TCP and UDP?", SELF),
    ("What does idempotent mean?", SELF),
    ("I'm tired today", SELF),
    ("Who wrote Pride and Prejudice?", SELF),
    ("Are you there?", SELF),
    # A correction stops the old task and resends it whole; an extra task does not
    (after("Check the status of the Hacker Hustle platform", "hermes", "Check the status of the Hacker Hustle platform",
           "No, not Hacker Hustle, it's HuggerHustle"), "hermes+replace"),
    (after("Email Minh that I'll be late", "hermes", "Email Minh that the user will be late",
           "Sorry, I said it wrong, it's Linh, not Minh"), "hermes+replace"),
    (after("Fix the login bug in the dashboard", "t3-code", "Fix the login bug in the dashboard",
           "I meant the admin dashboard, not the main one"), "t3-code+replace"),
    (after("What's the status of the OpsCom demo project?", "hermes", "Find the status of the OpsCom demo project",
           "Not the demo, the status page project"), "hermes+replace"),
    (after("Check the weather in Helsinki this weekend", "hermes", "Check the weather in Helsinki this weekend",
           "Also remind me to call the bank at 3pm"), "hermes"),
    (after("Add unit tests for the payment module", "t3-code", "Add unit tests for the payment module",
           "And after that, update the README too"), "t3-code"),
    # "Stop" while an agent works stops it (the speech check no longer mutes Jarvis for it)
    (after("Fix the login bug in the dashboard", "t3-code", "Fix the login bug in the dashboard", "Stop it."), "stop"),
    (after("Fix the login bug in the dashboard", "t3-code", "Fix the login bug in the dashboard", "No, stop that."), "stop"),
    (after("Find me a cheap flight to Hanoi", "hermes", "Find a cheap flight to Hanoi", "Cancel that."), "stop"),
    (after("Open the neuroparticles project", "t3-code", "Sync with master", "Stop that section."), "stop"),
    (after("Look up flights to Hanoi in December", "hermes", "Look up flights to Hanoi in December",
           "What does idempotent mean?"), SELF),
    # Only pointing at the screen looks at it; every case above that sets include_screen is a miss ("+screen")
    ("What does this popup on my screen mean?", "look"),
    ("Read me the error I'm looking at", "look"),
    ("Fix this error on my screen", "t3-code+screen"),
    ("Can you see what's wrong with the layout on my screen? Fix it.", "t3-code+screen"),
    # Multi-turn context: hold notes while explaining, side_question for a lookup in the middle, then act when asked
    ("Let me explain a bug in the Jarvis project first: when I click save on settings, the modal closes without saving.",
     "hold@jarvis"),
    (explaining(
        "Let me explain a weird UI bug first: clicking save closes the modal.",
        "Clicking save on settings closes the modal without saving",
        "To reproduce it, open profile settings, edit the email, and switch tabs — it crashes with a hook error.",
    ), "hold"),
    (explaining(
        "Let me explain a bug in Jarvis: editing email and switching tabs crashes with a hook error.",
        "Editing email in profile settings and switching tabs crashes with a hook error",
        "Wait, what version of Pipecat are we using in Jarvis right now?",
        project="jarvis",
    ), "t3-code@jarvis+task+side"),
    (explaining(
        "Let me explain a bug in Jarvis: editing email and switching tabs crashes with a hook error.",
        "Editing email in profile settings and switching tabs crashes with a hook error",
        "Okay, with Pipecat 1.15 I think unmounting causes that error. Find a way to fix it for me.",
        project="jarvis",
        middle=[
            {"role": "user", "content": "Wait, what version of Pipecat are we using right now?"},
            {"role": "assistant", "content": "It's using Pipecat 1.15.0."},
        ],
    ), "t3-code@jarvis+task"),
    # Speaker name corrections go to rename_speaker, never to send_agent_task or coding renames
    ("You saved my name wrong in the speakers list, change it to Sang", "rename"),
    ("Rename David to Daniel in the speaker list", "rename"),
    # "your dashboard" means Jarvis's own dashboard (omit project so brain routes to its board, not another dashboard repo)
    ("Fix the flickering bug in your dashboard", "t3-code"),
    ("Add a latency graph to your dashboard", "t3-code"),
]

# Vietnamese or mixed speech, with the vi prompt: routed the same, with English arguments
VI_CASES: list[tuple[str | list[dict], str]] = [
    ("Sửa lỗi ở thanh điều hướng giúp anh", "t3-code"),
    ("Fix cái bug login trong dashboard giúp mình", "t3-code"),
    ("Tiếp tục làm dự án Hugger Hustle platform", "t3-code@huggerhustle/platform-v2"),
    ("Gửi email cho Minh là anh sẽ đến muộn mười phút", "hermes"),
    ("Mở một phiên Claude Code mới", "claude+new"),
    ("Bạn khỏe không?", SELF),
    (after("Sửa lỗi đăng nhập ở dashboard", "t3-code", "Fix the login bug in the dashboard", "Dừng lại đi."), "stop"),
    (waiting("Claude needs approval: run git push to origin.", "Có, cứ làm đi."), "answer"),
    (waiting("T3 Code asks: Which database should I use, Postgres or SQLite?", "Dùng Postgres nhé."), "answer"),
    ("Cái lỗi trên màn hình này nghĩa là gì?", "look"),
    ("Sửa cái lỗi đang hiện trên màn hình giúp anh", "t3-code+screen"),
    ("Để anh kể cho nghe cái bug trong dự án Jarvis trước đã: lúc bấm lưu thì cửa sổ tự đóng luôn.", "hold@jarvis"),
    ("Tên anh trong danh sách speaker bị sai rồi, đổi lại thành Sang giúp anh", "rename"),
]


def outcome(message: dict, default_id: str | None) -> str:
    calls = message.get("tool_calls") or []
    if not calls:
        return SELF
    if calls[0]["function"]["name"] == "stop_agent":
        return "stop"  # stop_agent finds the busy agent itself when no id is given
    args = json.loads(calls[0]["function"].get("arguments") or "{}")
    if VIETNAMESE.search(args.get("task", "") + args.get("answer", "") + args.get("question", "") + args.get("note", "")):
        return "not English: " + (args.get("task") or args.get("answer") or args.get("question") or args.get("note"))
    if calls[0]["function"]["name"] == "look_at_screen":
        return "look"
    if calls[0]["function"]["name"] == "list_sessions":
        return "list"
    if calls[0]["function"]["name"] == "rename_speaker":
        return "rename"
    if calls[0]["function"]["name"] == "answer_agent":
        return "answer"  # answer_agent finds the waiting agent itself when no id is given
    label = projects_dir()[0].strip("/")
    raw_project = (args.get("project") or "").strip("/")
    if raw_project.lower().startswith(f"{label.lower()}/"):
        raw_project = raw_project[len(label) + 1 :].strip("/")
    project = f"@{raw_project.lower()}" if raw_project else ""
    if calls[0]["function"]["name"] == "hold_context":
        return "hold" + project + ("+clear" if args.get("clear") else "")
    named = (args.get("session") or "").strip().lower()
    # Opening a project or session names work only when the user did ("+task"); an invented task is a miss.
    task = "+task" if (project or named) and args.get("task", "").strip() else ""
    agent = args.get("agent_id") or default_id or "?"
    session = "" if not named else "+new" if named == "new" else "+session"
    screen = "+screen" if args.get("include_screen") else ""
    side = "+side" if args.get("side_question") else ""
    return agent + project + task + session + screen + ("+replace" if args.get("replace") else "") + side


async def main(rounds: int) -> int:
    if not settings.openrouter_api_key:
        print("OPENROUTER_API_KEY is not set.")
        return 2
    orchestrator = AgentOrchestrator(state=JarvisState())
    orchestrator.load_from_file(settings.watch_config_path)
    if not orchestrator.adapters:  # an empty roster tells the model no agent exists: the score would mean nothing
        print(f"No agents loaded from {settings.watch_config_path}; copy agents.json there (see README) and run again.")
        return 2
    # One orchestrator: every task goes to `brain`, which picks the runner. The case labels keep their
    # project/session/screen/replace suffixes; only the agent id changes.
    def expected(want: str) -> str:
        if want in (SELF, "stop", "look", "list", "answer", "rename"):
            return want
        return re.sub(r"^(?:t3-code|claude|opencode|hermes)", "brain", want)
    personality = load_soul().personality
    prompts = {lang: system_prompt(orchestrator, personality, language=lang) for lang in ("en", "vi")}
    tools = [{"type": "function", "function": t.to_default_dict()} for t in agent_tools()]
    misses: dict[str, str] = {}
    hits = calls = skipped = 0
    latencies: list[float] = []
    listed = {p.lower() for p in _projects}

    def routable(want: str) -> bool:
        """A case naming a project that is not on this machine cannot be routed; skip it instead of faking a miss."""
        match = re.search(r"@([^+]+)", want)
        return match is None or match.group(1).lower() in listed

    async with httpx.AsyncClient(timeout=30.0, headers={"Authorization": f"Bearer {settings.openrouter_api_key}"}) as http:
        for _ in range(rounds):
            for lang, text, want in [("en", t, w) for t, w in CASES] + [("vi", t, w) for t, w in VI_CASES]:
                if not routable(want):
                    skipped += 1
                    continue
                start = time.perf_counter()
                resp = await http.post(
                    f"{settings.openrouter_base_url.rstrip('/')}/chat/completions",
                    json={
                        "model": settings.executive_model,
                        "messages": [{"role": "system", "content": prompts[lang]},
                                     *([{"role": "user", "content": text}] if isinstance(text, str) else text)],
                        "tools": tools,
                        "reasoning": {"enabled": True},  # same as main.py
                        "provider": {"sort": "latency"},
                    },
                )
                latencies.append((time.perf_counter() - start) * 1000)
                resp.raise_for_status()
                got = outcome(resp.json()["choices"][0]["message"], orchestrator.default_agent_id)
                calls += 1
                if got == expected(want):
                    hits += 1
                else:
                    misses[text if isinstance(text, str) else text[-1]["content"]] = f"{got} (want {expected(want)})"
    for adapter in orchestrator.adapters.values():
        await adapter.stop()

    accuracy = hits / calls
    print(f"model {settings.executive_model}: {accuracy:.0%} over {calls} calls, median {statistics.median(latencies):.0f} ms"
          f" ({skipped} skipped: project not on this machine)")
    for text, got in misses.items():
        print(f"  miss: {text!r} -> {got}")
    return 0 if accuracy >= MIN_ACCURACY else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3)))
