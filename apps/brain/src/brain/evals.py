"""Harvest eval cases from real use into an editable `held_in` set and a sealed `held_out` set.

One case in five is pinned by `sha256(normalized words) % 5 == 0`; `sandbox.py` denies every worker that folder,
so an edit can never be tuned against the set that measures it. Cases dedupe on the same normalized text, so a
rephrased punctuation mark never leaks the same sentence into both splits.

Three suites: `intent` (a misheard sentence and the intent it really had), `brain_route` (a request the router
sent to the wrong place, with where the user then put it) and `voice_prompts` (what Whisper heard against what
`[Cleanup]` rewrote).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from loguru import logger

from brain.config import Settings
from brain.db import BrainDB, TraceRunRow
from brain.traces import scrub_text

SUITES = ("intent", "brain_route", "voice_prompts")
HELD_OUT_EVERY = 5  # one case in five, sealed
_WORD_RE = re.compile(r"\w+")
# The wrong target the router chose; the label is what the user then asked for.
_ROUTER_FRICTION = ("router_override", "replaced")


def normalized(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.lower()))


def split_of(text: str) -> str:
    """`held_out` | `held_in`: deterministic on the words, so the same sentence never moves."""
    digest = hashlib.sha256(normalized(text).encode("utf-8")).digest()
    return "held_out" if int.from_bytes(digest[:4], "big") % HELD_OUT_EVERY == 0 else "held_in"


def suite_path(home_dir: Path, split: str, suite: str) -> Path:
    return home_dir / "evals" / split / f"{suite}.jsonl"


def read_cases(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    cases: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            cases.append(record)
    return cases


def append_case(home_dir: Path, suite: str, case: dict[str, Any]) -> bool:
    """Writes one case to its split, unless the same sentence is already there; returns whether it was new."""
    text = str(case.get("input") or "")
    if not normalized(text):
        return False
    # shortcut: the suite file is re-read per case; a suite is tens of cases, batch it if it reaches thousands.
    path = suite_path(home_dir, split_of(text), suite)
    existing = {normalized(str(c.get("input") or "")) for c in read_cases(path)}
    if normalized(text) in existing:
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    path.chmod(0o600)
    return True


def harvest_eval_cases(db: BrainDB, cfg: Settings) -> dict[str, int]:
    """Adds whatever new evidence the traces and the feedback log hold; returns the count added per suite."""
    routes, prompts = _harvest_traces(db, cfg)
    return {"intent": _harvest_intent(cfg), "brain_route": routes, "voice_prompts": prompts}


def _harvest_intent(cfg: Settings) -> int:
    path = cfg.home_dir / "feedback" / "intent.jsonl"
    if not path.is_file():
        return 0
    decisions: dict[str, str] = {}
    corrections: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("id"):
            source = str((record.get("acted") or {}).get("source") or "")
            document = str((record.get("state") or {}).get("document") or "")
            if document and source in ("laya", "jev"):  # fixed phrases are rules, not a model miss
                decisions[str(record["id"])] = document
        elif record.get("correction_of"):
            corrections.append(record)
    added = 0
    for record in corrections:
        text = decisions.get(str(record.get("correction_of")))
        # `label` is what it was; `not_label` is what it was not, and only ever the opposite of a mute.
        label = record.get("label") or {"unmute": "mute", "mute": "unmute"}.get(str(record.get("not_label")))
        if not text or not label:
            continue
        added += append_case(
            cfg.home_dir, "intent", {"input": scrub_text(text), "intent": str(label)}
        )
    return added


def _harvest_traces(db: BrainDB, cfg: Settings) -> tuple[int, int]:
    rows = db.list_trace_runs(limit=400)
    by_id: dict[str, TraceRunRow] = {row.trace_id: row for row in rows}
    routes = prompts = 0
    for row in rows:
        if row.friction_kind in _ROUTER_FRICTION and row.superseded_by:
            fixed = by_id.get(row.superseded_by)
            if fixed is None or fixed.outcome != "ok":
                continue  # the user never put it right: no label to learn from
            routes += append_case(
                cfg.home_dir,
                "brain_route",
                {
                    "input": scrub_text(row.user_input),
                    "expected_tool": "delegate_project",
                    "project": fixed.repo or "",
                    "runner": fixed.runner or "t3",
                },
            )
        elif row.friction_kind == "asr_rewrite" and row.outcome == "ok":
            voice = _voice_stages(cfg, row.trace_path)
            stt, cleanup = str(voice.get("stt") or ""), str(voice.get("cleanup") or "")
            if not stt or not cleanup or normalized(stt) == normalized(cleanup):
                continue
            prompts += append_case(
                cfg.home_dir,
                "voice_prompts",
                {"input": scrub_text(stt), "cleanup": scrub_text(cleanup)},
            )
    return routes, prompts


def _voice_stages(cfg: Settings, trace_path: str) -> dict[str, Any]:
    if not trace_path:
        return {}
    try:
        record = json.loads((cfg.traces_dir / trace_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug(f"[Evals] no readable trace at {trace_path!r}")
        return {}
    return record.get("voice") or {} if isinstance(record, dict) else {}
