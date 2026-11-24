"""Benchmark the prompts that decide what Jarvis says, against invariants instead of a judge.

Covers `TranscriptCleaner.clean`, the retell prompt (`AGENT_REPORT_PROMPT` or `SYSTEM.md`'s `## Retell`) and
the "still working" lines, including any active `SYSTEM.md` override. Every check is deterministic: no second
model grades the first.

Usage: uv run python scripts/bench_voice_prompts.py [--split held_in|held_out|all] [--dump-results PATH]
Needs OPENROUTER_API_KEY (~10 paid calls). Exits 1 below MIN_PASS_RATE.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import httpx

from jarvis_voice.cleanup import TranscriptCleaner, keep_cleaned
from jarvis_voice.config import JARVIS_HOME, settings
from jarvis_voice.system_prompts import build_retell_prompt, load_overrides
from jarvis_voice.waiting_lines import WaitingLines

MIN_PASS_RATE = 0.95
MAX_RETELL_WORDS = 20
MAX_OPTION_PHRASES = 4
MAX_WAITING_CHARS = 90

PROGRESS_CLAIM = re.compile(
    r"\b(almost done|nearly there|almost there|\d+\s?%|found it|a few more minutes|just finishing|"
    r"should be done|in a minute)\b",
    re.IGNORECASE,
)
AGENT_NAME = re.compile(r"\b(T3 Code|OpenCode|Claude Code|the agent|the board|pi)\b", re.IGNORECASE)
MARKDOWN = re.compile(r"`|^#{1,6}\s|\*\*|\]\(", re.MULTILINE)
VIETNAMESE = re.compile(r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", re.IGNORECASE)

# (what Whisper heard, a fragment the cleanup must keep, a fragment it must drop)
CLEANUP_CASES: list[tuple[str, str, str]] = [
    ("um so like make it 42, sorry, 24", "24", "42"),
    ("fix the bug in pyproject.toml", "pyproject.toml", ""),
    ("open the opscom/status project", "opscom/status", ""),
    ("no, sorry, use Postgres not SQLite", "Postgres", ""),
    ("Sửa lỗi đăng nhập giúp anh", "đăng nhập", ""),
]

# (an agent's finished turn, whether it asks the user to decide)
RETELL_CASES: list[tuple[str, bool]] = [
    (
        "## Fixed the navbar\n\n- Updated `Nav.tsx` to keep the link on one line.\n"
        "- Ran `pnpm test`: 34 passed.\n\nAll done.",
        False,
    ),
    (
        "## Options\n\n1. Use Postgres for the queue: durable, needs a migration.\n"
        "2. Use Redis: faster, loses jobs on restart.\n3. Keep SQLite: no new service, no guarantee.\n"
        "4. Use SQS: managed, costs money.\n5. Use Kafka: heaviest, best throughput.\n\n"
        "Which one should I take?",
        True,
    ),
]

WAITING_TASKS = ["upgrade the brain's router", "fix the flaky websocket test"]


def parse_args(argv: list[str]) -> tuple[str, Path | None]:
    split = "held_in"
    dump: Path | None = None
    args = list(argv)
    while args:
        flag = args.pop(0)
        if flag == "--split":
            split = args.pop(0)
        elif flag == "--dump-results":
            dump = Path(args.pop(0))
        else:
            raise SystemExit(f"unknown argument {flag!r}")
    if split not in ("held_in", "held_out", "all"):
        raise SystemExit(f"--split must be held_in, held_out or all (got {split!r})")
    return split, dump


def words(text: str) -> int:
    return len(re.findall(r"\w+", text))


def retell_ok(reply: str, asks: bool) -> bool:
    if not reply.strip():
        return False
    if MARKDOWN.search(reply) or AGENT_NAME.search(reply):
        return False
    if asks:
        # One phrase per heading, at most four, then its question (AGENT_REPORT_PROMPT).
        if not reply.rstrip().endswith("?"):
            return False
        return words(reply) <= MAX_OPTION_PHRASES * 12 + 20
    return words(reply) <= MAX_RETELL_WORDS


def waiting_ok(line: str) -> bool:
    return (
        0 < len(line) <= MAX_WAITING_CHARS
        and not PROGRESS_CLAIM.search(line)
        and not AGENT_NAME.search(line)
    )


def overlap(left: str, right: str) -> float:
    """Share of the known rewrite's words that the cleanup kept, so a harvest is scored, not just sanity-checked."""
    old = set(re.findall(r"\w+", left.lower()))
    return len(old & set(re.findall(r"\w+", right.lower()))) / len(old) if old else 0.0


async def run_cleanup(http: httpx.AsyncClient, cases: list[dict[str, str]]) -> dict[str, bool]:
    cleaner = TranscriptCleaner(prompt=load_overrides().get("cleanup"))
    results: dict[str, bool] = {}
    try:
        for case in cases:
            heard = case["input"]
            got = await cleaner.clean(heard)
            if case.get("cleanup"):  # harvested from a real turn: close to what [Cleanup] actually did
                results[heard] = keep_cleaned(heard, got) and overlap(case["cleanup"], got) >= 0.6
                continue
            keep = case.get("keep", "")
            drop = case.get("drop", "")
            ok = bool(got.strip()) and (not keep or keep in got) and (not drop or drop not in got)
            if VIETNAMESE.search(heard):
                ok = ok and bool(VIETNAMESE.search(got))  # never translate
            results[heard] = ok
    finally:
        await cleaner.aclose()
    return results


async def run_retell(http: httpx.AsyncClient, cases: list[dict[str, str]]) -> dict[str, bool]:
    prompt = build_retell_prompt(load_overrides())
    results: dict[str, bool] = {}
    for case in cases:
        report = case["input"]
        asks = case.get("asks", "false") == "true"
        resp = await http.post(
            "/chat/completions",
            json={
                "model": settings.executive_model,
                "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": report}],
                "reasoning": {"enabled": True},
                "provider": {"sort": "latency"},
            },
        )
        resp.raise_for_status()
        results[report] = retell_ok(resp.json()["choices"][0]["message"]["content"] or "", asks)
    return results


async def run_waiting(http: httpx.AsyncClient, tasks: list[str]) -> dict[str, bool]:
    prompt = load_overrides().get("waiting")  # None: the shipped WAITING_PROMPT
    results: dict[str, bool] = {}
    for task in tasks:
        generator = WaitingLines(task, http=http, prompt=prompt)
        generator.start()
        if generator._refill is not None:  # the batch itself, not the base-line fallback
            await generator._refill
        lines = list(generator._pool)
        results[task] = bool(lines) and all(waiting_ok(line) for line in lines)
    return results


def seed_cases() -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    cleanup_cases = [
        {"input": heard, "keep": keep, "drop": drop} for heard, keep, drop in CLEANUP_CASES
    ]
    retell_cases = [
        {"input": report, "asks": "true" if asks else "false"} for report, asks in RETELL_CASES
    ]
    return cleanup_cases, retell_cases, list(WAITING_TASKS)


def harvested_cases(split: str) -> list[dict[str, str]]:
    splits = ["held_in"] if split == "held_in" else ["held_out"] if split == "held_out" else ["held_in", "held_out"]
    cases: list[dict[str, str]] = []
    for name in splits:
        path = JARVIS_HOME / "evals" / name / "voice_prompts.jsonl"
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("input"):
                cases.append({"input": str(record["input"]), "cleanup": str(record.get("cleanup") or "")})
    return cases


async def main(split: str, dump: Path | None) -> int:
    if not settings.openrouter_api_key:
        print("OPENROUTER_API_KEY is not set.")
        return 2
    cleanup_cases, retell_cases, waiting_tasks = seed_cases() if split in ("held_in", "all") else ([], [], [])
    cleanup_cases += harvested_cases(split)
    results: dict[str, bool] = {}
    async with httpx.AsyncClient(
        base_url=settings.openrouter_base_url.rstrip("/"),
        timeout=30.0,
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
    ) as http:
        if cleanup_cases:
            results |= await run_cleanup(http, cleanup_cases)
        if retell_cases:
            results |= await run_retell(http, retell_cases)
        if waiting_tasks:
            results |= await run_waiting(http, waiting_tasks)
    total = len(results)
    rate = sum(results.values()) / total if total else 0.0
    print(f"voice prompts: {rate:.0%} over {total} cases")
    for case, ok in results.items():
        if not ok:
            print(f"  miss: {case[:90]!r}")
    if dump is not None:
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(json.dumps({"split": split, "cases": results}, indent=2), encoding="utf-8")
    print(f"METRIC: {rate:.4f}")
    print(f"METRICS: {json.dumps({'pass_rate': round(rate, 4), 'cases': total})}")
    return 0 if rate >= MIN_PASS_RATE else 1


if __name__ == "__main__":
    wanted_split, dump_path = parse_args(sys.argv[1:])
    sys.exit(asyncio.run(main(wanted_split, dump_path)))
