"""Benchmark what Jarvis hears: runs the real ExecutiveFilter.classify_user_utterance on a fixed sentence set.

Usage: uv run python scripts/bench_intent.py [rounds]   (needs OPENROUTER_API_KEY; ~100 paid calls at 3 rounds)
Exits 1 when "is it for Jarvis?" accuracy drops below MIN_ACCURACY.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time

from jarvis.executive_filter import ExecutiveFilter
from jarvis.state import UserIntent

MIN_ACCURACY = 0.97  # INTENT_QUESTION scored 97% on 2026-11-02; a wording change may not go below it

A, I, M, U = (UserIntent.ADDRESSED_TO_JARVIS, UserIntent.IGNORE_BACKGROUND, UserIntent.MUTE, UserIntent.UNMUTE)
CASES: list[tuple[str, UserIntent]] = [
    ("Jarvis, what is OpenCode doing right now?", A),
    ("Build a login page with email and password", A),
    ("Tell Claude to run the tests", A),
    ("Yes, go ahead", A),
    ("Use Postgres", A),
    ("Jarvis, stop Claude", A),
    ("What's the status of the frontend?", A),
    ("Fix the bug in the navbar", A),
    ("How are you?", A),
    ("Good morning", A),
    ("What's up?", A),
    ("Thanks, that's great", A),
    ("I'm tired today", A),
    ("Are you there?", A),
    ("Tell me a joke", A),
    ("What's on my calendar tomorrow?", A),
    ("Email Minh that I'll be ten minutes late", A),
    ("Remind me to call the bank at 3pm", A),
    ("Find me a good ramen place nearby", A),
    ("Hey honey, is dinner ready?", I),
    ("Hmm, let me think about that", I),
    ("Uh, yeah, okay", I),
    ("I'll call you back in five minutes", I),
    ("Did you see the game last night?", I),  # known miss: Jev answers it; honestly ambiguous
    ("So anyway, the meeting is at three", I),
    ("Can you pass me the salt?", I),
    ("Mom, I'm on a call", I),
    ("Honey, can you pass the salt?", I),
    ("Mom, I'll call you back", I),
    ("Hmm, let me think", I),
    ("um yeah okay", I),
    ("Yeah babe, I'll be down in five minutes", I),
    ("Jarvis, be quiet for a while", M),
    ("Stop talking please", M),
    ("Okay Jarvis, you can speak again", U),
    ("Alright, go ahead and talk now", U),
]


async def main(rounds: int) -> int:
    filt = ExecutiveFilter()
    if not filt.api_key:
        print("OPENROUTER_API_KEY is not set.")
        return 2
    misses: dict[str, str] = {}
    hits = calls = 0
    latencies: list[float] = []
    try:
        for _ in range(rounds):
            for text, want in CASES:
                start = time.perf_counter()
                got = await filt.classify_user_utterance(text)
                latencies.append((time.perf_counter() - start) * 1000)
                calls += 1
                # What matters is whether the sentence reaches Jarvis, so MUTE/UNMUTE/ADDRESSED all count as "heard".
                if (got != I) == (want != I):
                    hits += 1
                else:
                    misses[text] = got.value
    finally:
        await filt.aclose()

    accuracy = hits / calls
    print(f"model {filt.model}: {accuracy:.0%} over {calls} calls, median {statistics.median(latencies):.0f} ms")
    for text, got in misses.items():
        print(f"  miss: {text!r} -> {got}")
    return 0 if accuracy >= MIN_ACCURACY else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3)))
