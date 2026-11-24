"""Choosing between competing harness candidates on more than accuracy.

A Pareto filter over `(accuracy, p50 latency, cost)`, with two hard rules from the plan: a candidate that
regressed a sealed case is out, and no candidate may buy a sliver of accuracy with a jump in voice latency.
The numbers come from the `METRICS: {...}` line the benchmarks print; nothing here calls a model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

MAX_LATENCY_FOR_ACCURACY_MS = 150.0  # more voice latency than this needs a real accuracy win
MIN_ACCURACY_GAIN = 0.02  # what counts as a real accuracy win
_ACCURACY_COST = 0.0001  # per ms, in the scalar tie-break
_USD_COST = 0.05  # per extra dollar, in the scalar tie-break


@dataclass(frozen=True)
class Candidate:
    id: str
    accuracy: float
    p50_ms: float
    cost_usd: float
    regressions: int = 0


def parse_metrics(output: str | None) -> dict[str, float]:
    """The last `METRICS: {...}` line a benchmark printed, or `{}` when it printed none."""
    if not output:
        return {}
    found: dict[str, float] = {}
    for line in output.splitlines():
        if not line.startswith("METRICS:"):
            continue
        try:
            raw = json.loads(line.split(":", 1)[1].strip())
        except ValueError:
            continue
        if isinstance(raw, dict):
            found = {
                str(key): float(value)
                for key, value in raw.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
    return found


def dominates(left: Candidate, right: Candidate) -> bool:
    """True when `left` is at least as good on every axis and better on one."""
    better_or_equal = (
        left.accuracy >= right.accuracy
        and left.p50_ms <= right.p50_ms
        and left.cost_usd <= right.cost_usd
    )
    strictly_better = (
        left.accuracy > right.accuracy
        or left.p50_ms < right.p50_ms
        or left.cost_usd < right.cost_usd
    )
    return better_or_equal and strictly_better


def utility(candidate: Candidate, baseline: Candidate | None = None) -> float:
    """One number for ranking survivors; accuracy leads, latency and cost only break ties."""
    base_ms = baseline.p50_ms if baseline else 0.0
    base_usd = baseline.cost_usd if baseline else 0.0
    return (
        candidate.accuracy
        - _ACCURACY_COST * max(0.0, candidate.p50_ms - base_ms)
        - _USD_COST * max(0.0, candidate.cost_usd - base_usd)
    )


def select_winner(
    candidates: list[Candidate],
    *,
    baseline: Candidate | None = None,
    floor: float = 0.0,
) -> Candidate | None:
    """The candidate worth keeping, or None when the baseline already beats them all."""
    usable = [c for c in candidates if c.regressions == 0 and c.accuracy >= floor]
    if baseline is not None:
        usable = [
            c
            for c in usable
            if not (
                c.p50_ms - baseline.p50_ms > MAX_LATENCY_FOR_ACCURACY_MS
                and c.accuracy - baseline.accuracy < MIN_ACCURACY_GAIN
            )
        ]
    front = [c for c in usable if not any(dominates(other, c) for other in usable if other.id != c.id)]
    if not front:
        return None
    winner = max(front, key=lambda c: utility(c, baseline))
    if baseline is not None and utility(winner, baseline) <= utility(baseline, baseline):
        return None  # nothing earned the change
    return winner


def render_tournament(candidates: list[Candidate], winner: Candidate | None) -> str:
    """One line per candidate, for the report the owner reads."""
    lines = [
        f"- {c.id}: accuracy {c.accuracy:.2f}, p50 {c.p50_ms:.0f} ms, ${c.cost_usd:.3f}"
        + (f", {c.regressions} regression(s)" if c.regressions else "")
        for c in sorted(candidates, key=lambda c: -c.accuracy)
    ]
    lines.append(f"winner: {winner.id}" if winner else "winner: none (the baseline stays)")
    return "\n".join(lines)
