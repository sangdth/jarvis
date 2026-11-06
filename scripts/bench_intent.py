"""Benchmark what Jarvis hears: runs the real ExecutiveFilter.classify_user_utterance on a fixed sentence set.

Usage: uv run python scripts/bench_intent.py [rounds]   (needs OPENROUTER_API_KEY; ~160 paid calls at 3 rounds)
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

# One letter per label keeps the case table readable.
A, I, M, U = (UserIntent.ADDRESSED_TO_JARVIS, UserIntent.IGNORE_BACKGROUND, UserIntent.MUTE, UserIntent.UNMUTE)  # noqa: E741
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
    # "Stop" stops the agent's work, it does not mute Jarvis. Written as Whisper writes them: with the period,
    # "Stop it." was MUTE 5/5 (2026-11-04, live "Stop that section." muted Jarvis instead of stopping T3)
    ("Stop.", A),
    ("Stop it.", A),
    ("Stop that.", A),
    ("No, stop that.", A),
    ("Stop that section.", A),
    ("Stop the T3 session.", A),
    ("Cancel that.", A),
    ("Shut up.", M),
    ("Be quiet.", M),
    ("Jarvis, be quiet for a while", M),
    ("Stop talking please", M),
    ("Okay Jarvis, you can speak again", U),
    ("Alright, go ahead and talk now", U),
    # Vietnamese and mixed speech (Whisper detects the language per sentence)
    ("Jarvis ơi, sửa lỗi đăng nhập giúp anh", A),
    ("Fix cái bug login trong dashboard giúp mình", A),
    ("Bạn khỏe không?", A),
    ("Chào buổi sáng", A),
    ("Có, cứ làm đi", A),
    ("Dừng lại đi.", A),
    ("Lịch của anh ngày mai có gì?", A),
    ("Em ơi, cơm chín chưa?", I),
    ("Mẹ ơi, con đang họp", I),
    ("Để anh nghĩ đã", I),
    ("Im đi.", M),
    ("Jarvis, im lặng một chút", M),
    ("Nói được rồi", U),
]
# (what Jarvis just said, what was heard): answers, often garbled by Whisper, and room talk right after Jarvis spoke.
# The answers are real misses from the 2026-11-02..06 logs (docs/research/voice-recognition-misses.md).
REPLY_CASES: list[tuple[str, str, UserIntent]] = [
    (
        "Which project should the new section go in, the platform one in Opscom, or somewhere else?",
        "the tree to continue walking.",
        A,
    ),
    ("T3 Code is still waiting to hear which page or thread you meant by new section.", "Ken sorry", A),
    (
        "Sorry, shoot what up to you? The status of the Genes fix, or something else?",
        "Yes, you should have got me how the... The tree is walking under the...",
        A,
    ),
    ("Want me to push it and open a PR?", "No.", A),
    ("Should I run the tests now or later?", "now.", A),
    ("I've sent that over, I'll tell you when it's done.", "Honey, can you grab the kids from school?", I),
    ("Hermes found six projects in Happy Voxel.", "Mom, I'll call you back in five minutes.", I),
    ("Done, the PR is up.", "Minh, did you see the game last night?", I),
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
            for last_said, text, want in [(None, t, w) for t, w in CASES] + REPLY_CASES:
                start = time.perf_counter()
                got = await filt.classify_user_utterance(text, recent_jarvis_question=last_said)
                latencies.append((time.perf_counter() - start) * 1000)
                calls += 1
                # Heard vs ignored is what matters most; on top, mute and unmute must be exact: "Stop it." read as
                # MUTE silences Jarvis instead of stopping the agent, and a missed "shut up" keeps it talking.
                if (got != I) == (want != I) and (got == M) == (want == M) and (got == U) == (want == U):
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
