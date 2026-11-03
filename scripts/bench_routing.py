"""Benchmark where Jarvis sends a request: runs the real system prompt, roster and tools on EXECUTIVE_MODEL.

Usage: uv run python scripts/bench_routing.py [rounds]   (needs OPENROUTER_API_KEY; ~100 paid calls at 3 rounds)
Exits 1 when accuracy drops below MIN_ACCURACY.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time

import httpx

from jarvis.agent_watchers import AgentOrchestrator
from jarvis.config import settings
from jarvis.main import JARVIS_EXECUTIVE_SYSTEM_PROMPT, agent_roster, agent_tools
from jarvis.state import JarvisState

MIN_ACCURACY = 0.95  # 99% on 2026-11-02 (DeepSeek V4.1 Flash, thinking off)

SELF = "answer yourself"  # no tool call
CASES: list[tuple[str, str]] = [
    ("Build a login page with email and password", "claude"),
    ("Fix the bug in the navbar", "claude"),
    ("Add unit tests for the payment module", "claude"),
    ("Refactor the auth service to use JWT", "claude"),
    ("Why is the build failing?", "claude"),
    ("Rename the user table column to email_address", "claude"),
    ("Write a Python script that resizes all images in a folder", "claude"),
    ("Have OpenCode add dark mode", "opencode"),
    ("Ask OpenCode to update the README", "opencode"),
    ("Tell Claude to run the tests", "claude"),
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
    ("How many projects do we have in OpsCom?", "hermes"),
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
]


def outcome(message: dict, default_id: str | None) -> str:
    calls = message.get("tool_calls") or []
    if not calls:
        return SELF
    args = json.loads(calls[0]["function"].get("arguments") or "{}")
    return args.get("agent_id") or default_id or "?"


async def main(rounds: int) -> int:
    if not settings.openrouter_api_key:
        print("OPENROUTER_API_KEY is not set.")
        return 2
    orchestrator = AgentOrchestrator(state=JarvisState())
    orchestrator.load_from_file(settings.watch_config_path)
    system = JARVIS_EXECUTIVE_SYSTEM_PROMPT + agent_roster(orchestrator)
    tools = [{"type": "function", "function": t.to_default_dict()} for t in agent_tools()]
    misses: dict[str, str] = {}
    hits = calls = 0
    latencies: list[float] = []
    async with httpx.AsyncClient(timeout=30.0, headers={"Authorization": f"Bearer {settings.openrouter_api_key}"}) as http:
        for _ in range(rounds):
            for text, want in CASES:
                start = time.perf_counter()
                resp = await http.post(
                    f"{settings.openrouter_base_url.rstrip('/')}/chat/completions",
                    json={
                        "model": settings.executive_model,
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
                        "tools": tools,
                        "reasoning": {"enabled": False},  # same as main.py
                        "provider": {"sort": "latency"},
                    },
                )
                latencies.append((time.perf_counter() - start) * 1000)
                resp.raise_for_status()
                got = outcome(resp.json()["choices"][0]["message"], orchestrator.default_agent_id)
                calls += 1
                if got == want:
                    hits += 1
                else:
                    misses[text] = f"{got} (want {want})"
    for adapter in orchestrator.adapters.values():
        await adapter.stop()

    accuracy = hits / calls
    print(f"model {settings.executive_model}: {accuracy:.0%} over {calls} calls, median {statistics.median(latencies):.0f} ms")
    for text, got in misses.items():
        print(f"  miss: {text!r} -> {got}")
    return 0 if accuracy >= MIN_ACCURACY else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3)))
