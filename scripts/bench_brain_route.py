"""Benchmark where the brain's router sends one request, on the real `ROUTER_PROMPT` and tools.

Usage: uv run python scripts/bench_brain_route.py [rounds] [--split held_in|held_out|all]
       [--dump-results PATH] [--no-seed]
Needs OPENROUTER_API_KEY (~15 paid calls per round). Exits 1 below MIN_ACCURACY or over MAX_P50_MS.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import tempfile
from pathlib import Path

from brain.config import Settings
from brain.db import BrainDB
from brain.evals import normalized, read_cases, suite_path
from brain.orchestrator import BrainOrchestrator
from brain.reconciler import Reconciler
from brain.telemetry import TelemetryHub

MIN_ACCURACY = 0.95
MAX_P50_MS = 800  # the voice waits on this call before it can answer

# (what the user said, the tool that must be chosen, extra fields that must match)
# No case names a project: folder resolution is machine-dependent and is scored from the held-in set instead.
SEED_CASES: list[tuple[str, str, dict[str, str]]] = [
    ("Build a settings page with email and password", "delegate_project", {}),
    ("Fix the failing checkout test", "delegate_project", {}),
    ("Refactor the auth service to use JWT", "delegate_project", {}),
    ("Add unit tests for the payment module", "delegate_project", {}),
    ("Rename the user table column to email_address", "delegate_project", {}),
    ("Have Claude Code rename the user table column", "delegate_project", {"runner": "claude"}),
    ("Ask OpenCode to update the README", "delegate_project", {"runner": "opencode"}),
    ("Start a fresh thread for this and fix the flaky websocket test", "delegate_project", {"session": "new"}),
    ("What is the capital of Portugal?", "ask_ephemeral_pi", {}),
    ("How many bytes are in a mebibyte?", "ask_ephemeral_pi", {}),
    ("Check whether port 8643 is listening on this machine", "ask_ephemeral_pi", {}),
    ("What does idempotent mean?", "ask_ephemeral_pi", {}),
    ("What sessions do I have open right now?", "list_sessions", {}),
    ("List my coding threads", "list_sessions", {}),
    ("Queue an overnight job to upgrade Jarvis", "queue_board_task", {}),
    ("Run this autonomously overnight: keep fixing the flaky tests", "queue_board_task", {}),
    ("Set a goal to cut the build time in half", "queue_board_task", {}),
    ("Fix the speakers pane bug in your dashboard", "queue_board_task", {"repo": "jarvis"}),
    ("Add a latency graph to Jarvis's dashboard (apps/dashboard)", "queue_board_task", {"repo": "jarvis"}),
    ("Fix the login bug in the dashboard", "delegate_project", {}),
    ("How many project folders do I have?", "reply_direct", {}),
]


def parse_args(argv: list[str]) -> tuple[int, str, Path | None, bool]:
    rounds = 1
    split = "held_in"
    dump: Path | None = None
    seed = True
    args = list(argv)
    if args and not args[0].startswith("--"):
        rounds = int(args.pop(0))
    while args:
        flag = args.pop(0)
        if flag == "--split":
            split = args.pop(0)
        elif flag == "--dump-results":
            dump = Path(args.pop(0))
        elif flag == "--no-seed":
            seed = False
        else:
            raise SystemExit(f"unknown argument {flag!r}")
    if split not in ("held_in", "held_out", "all"):
        raise SystemExit(f"--split must be held_in, held_out or all (got {split!r})")
    return rounds, split, dump, seed


def case_id(text: str) -> str:
    return normalized(text)


def load_cases(cfg: Settings, split: str, seed: bool) -> list[dict[str, str]]:
    cases: list[dict[str, str]] = []
    if seed and split in ("held_in", "all"):
        cases += [
            {"input": text, "expected_tool": tool, **extra} for text, tool, extra in SEED_CASES
        ]
    splits = ["held_in"] if split == "held_in" else ["held_out"] if split == "held_out" else ["held_in", "held_out"]
    for name in splits:
        for case in read_cases(suite_path(cfg.home_dir, name, "brain_route")):
            cases.append({str(k): str(v) for k, v in case.items()})
    return cases


def matches(choice: dict, case: dict[str, str]) -> bool:
    if str(choice.get("name")) != case["expected_tool"]:
        return False
    args = choice.get("arguments") or {}
    if case.get("runner") and str(args.get("runner") or "t3") != case["runner"]:
        return False
    if case.get("session") and str(args.get("session") or "").strip().lower() != case["session"]:
        return False
    project = case.get("project", "").strip("/").lower()
    if project and str(args.get("project") or "").strip("/").lower() != project:
        return False
    repo = case.get("repo", "").strip("/").lower()
    if repo and str(args.get("repo") or "").strip("/").lower() != repo:
        return False
    return True


async def main(rounds: int, split: str, dump: Path | None, seed: bool) -> int:
    # A throwaway state home: the benchmark must never recover timers or sync skills in the live board.
    cfg = Settings(home_dir=Path(tempfile.mkdtemp(prefix="bench-route-")))
    if not cfg.openrouter_api_key:
        print("OPENROUTER_API_KEY is not set.")
        return 2
    cases = load_cases(cfg, split, seed)
    if not cases:
        print(f"no {split} cases: run the Improver once so evals.py can harvest them, or drop --no-seed")
        return 2
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    orchestrator = BrainOrchestrator(cfg, db, Reconciler(db, cfg, hub=TelemetryHub()), runners={})
    results: dict[str, bool] = {}
    misses: dict[str, str] = {}
    latencies: list[float] = []
    try:
        for _ in range(rounds):
            for case in cases:
                choice, route_ms = await orchestrator.decide(case["input"])
                latencies.append(route_ms)
                ok = matches(choice, case)
                results[case_id(case["input"])] = ok
                if not ok:
                    args = choice.get("arguments") or {}
                    misses[case["input"]] = (
                        f"{choice.get('name')}"
                        f" runner={args.get('runner') or ''} project={args.get('project') or ''}"
                        f" session={args.get('session') or ''} (want {case['expected_tool']}"
                        f" {case.get('runner') or ''} {case.get('project') or ''} {case.get('session') or ''})"
                    ).replace("  ", " ")
    finally:
        await orchestrator.close()
    total = len(results)
    accuracy = sum(results.values()) / total if total else 0.0
    p50 = statistics.median(latencies) if latencies else 0.0
    print(
        f"router {cfg.orchestrator_model or 'default'}: {accuracy:.0%} over {total} cases, "
        f"p50 {p50:.0f} ms, max {max(latencies):.0f} ms"
    )
    for text, got in misses.items():
        print(f"  miss: {text!r} -> {got}")
    if dump is not None:
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(json.dumps({"split": split, "cases": results}, indent=2), encoding="utf-8")
    print(f"METRIC: {accuracy:.4f}")
    print(f"METRICS: {json.dumps({'accuracy': round(accuracy, 4), 'p50_ms': round(p50), 'cases': total})}")
    return 0 if accuracy >= MIN_ACCURACY and p50 <= MAX_P50_MS else 1


if __name__ == "__main__":
    rounds, wanted_split, dump_path, use_seed = parse_args(sys.argv[1:])
    sys.exit(asyncio.run(main(rounds, wanted_split, dump_path, use_seed)))
