"""Durable, local, agent-readable traces: the experience the Improver learns from.

`TelemetryHub` stays a memory-only bus for the live cockpit. This module drains it in the background into
three layers the offline `pi` Improver can actually read inside its Seatbelt sandbox:

1. `traces/<YYYY-MM-DD>/spans.jsonl` and `trace-<trace_id>.json` — one file per turn/run, OpenTelemetry
   `gen_ai.*` field names, pruned by day (no SQLite vacuum, no extra daemon).
2. `state.db` `trace_runs` + `trace_fts` — a ~350-byte row per turn/run with outcome, implicit friction,
   `git_sha`/`prompt_sha` and cost, so regressions are detected statistically across commits.
3. `traces/analysis/overview.md` + `surfaces/<surface>.md` — the progressive-disclosure digest the Improver
   reads first, then drills into raw JSON on demand.

Persistence never blocks the audio path: the bus listener only enqueues; one background task redacts, writes
and indexes. Pairing codes, credentials and unaddressed room speech never reach disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from loguru import logger

from brain.config import Settings
from brain.db import BrainDB, TraceRunRow
from brain.sandbox import SECRET_ENV
from brain.telemetry import (
    EVENT_ATTEMPT_FINISHED,
    EVENT_BRAIN_ROUTE,
    EVENT_HTTP_COMPLETED,
    EVENT_PAIR_REQUESTED,
    EVENT_RUN_STATUS,
    EVENT_VOICE_CLEANUP,
    EVENT_VOICE_DISPATCH,
    EVENT_VOICE_INTENT,
    EVENT_VOICE_LATENCY,
    EVENT_VOICE_LEVEL,
    EVENT_VOICE_MEMORY,
    EVENT_VOICE_REQUEST,
    EVENT_VOICE_RETELL,
    EVENT_VOICE_RETURN,
    EVENT_VOICE_SPEAKER,
    EVENT_VOICE_STT,
    EVENT_VOICE_TOOL,
    EVENT_VOICE_TTS,
    TelemetryHub,
)
from jarvis_core.home import git_version

SurfaceKind = Literal["prompt", "router", "tool", "skill", "memory", "weights"]


@dataclass(frozen=True)
class HarnessSurface:
    """One editable component: the files it owns and the benchmark that must pass before an edit lands."""

    id: str
    kind: SurfaceKind
    editable_files: tuple[str, ...]
    gate_cmd: str | None = None


# The declared action space (AHE's component observability): one failure maps to one component.
HARNESS_SURFACES: dict[str, HarnessSurface] = {
    "voice_intent_data": HarnessSurface(
        "voice_intent_data",
        "weights",
        ("apps/voice/src/jarvis_voice/executive_filter.py",),
        "uv run python scripts/bench_intent.py 1",
    ),
    "whisper_asr_data": HarnessSurface(
        "whisper_asr_data",
        "weights",
        ("apps/voice/src/jarvis_voice/cleanup.py", "SYSTEM.md"),
        "uv run pytest apps/voice/tests",
    ),
    "voice_prompts": HarnessSurface(
        "voice_prompts",
        "prompt",
        (
            "apps/voice/src/jarvis_voice/prompts.py",
            "apps/voice/src/jarvis_voice/agent_tools.py",
            "apps/voice/src/jarvis_voice/languages.json",
            "SYSTEM.md",
        ),
        "uv run python scripts/bench_routing.py 1",
    ),
    "brain_router": HarnessSurface(
        "brain_router",
        "router",
        ("apps/brain/src/brain/orchestrator.py",),
        "uv run pytest apps/brain/tests/test_orchestrator.py",
    ),
    "builder_harness": HarnessSurface(
        "builder_harness",
        "tool",
        ("apps/brain/src/brain/reconciler.py",),
        "uv run pytest apps/brain/tests",
    ),
    "skills": HarnessSurface("skills", "skill", ("skills/*/SKILL.md",)),
    "memory": HarnessSurface("memory", "memory", ("memory/MEMORY.md", "memory/repos/*.md")),
}

_DROP_EVENTS = frozenset({EVENT_VOICE_LEVEL, "heartbeat", "voice.thinking"})
_SECRET_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
)
_MAX_TEXT = 4000
_SUMMARY_CHARS = 280
_FLUSH_AFTER_SECS = 30 * 60
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def causal_signature(cause: str, behavior: str, mechanism: str) -> str:
    """Self-Harness `phi(r)`: verifier cause, causal agent behaviour, reusable mechanism."""
    joined = f"{cause}|{behavior}|{mechanism}"
    return joined[:160]


def classify_surface(
    outcome: str,
    *,
    repeated_error_tools: tuple[str, ...] = (),
    used_skills: bool = False,
) -> tuple[str, str]:
    """Map a board attempt's outcome to one harness surface and a causal signature."""
    if outcome in ("pushed", "decomposed", "deferred_approval"):
        return "", ""
    if repeated_error_tools:
        return "builder_harness", causal_signature(outcome, f"tool={repeated_error_tools[0]}", "tool_interface")
    if outcome == "failed_verify":
        return "builder_harness", causal_signature(outcome, "verify_gate", "wrong_or_incomplete_fix")
    if outcome in ("timed_out", "errored"):
        return "builder_harness", causal_signature(outcome, "runtime", "budget_or_prompt")
    if used_skills:
        return "skills", causal_signature(outcome, "skill_loaded", "skill_guidance")
    return "builder_harness", causal_signature(outcome, "plan", "wrong_plan")


def word_change_ratio(left: str, right: str) -> float:
    """Share of `left`'s words that no longer appear in `right` (order-insensitive, cheap)."""
    old = set(re.findall(r"\w+", left.lower()))
    if not old:
        return 0.0
    new = set(re.findall(r"\w+", right.lower()))
    return len(old - new) / len(old)


def _secret_values() -> list[str]:
    return [value for name in SECRET_ENV if (value := os.environ.get(name)) and len(value) >= 6]


def _scrub(text: str, secrets: list[str]) -> str:
    for value in secrets:
        if value in text:
            text = text.replace(value, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _clean(value: Any, secrets: list[str], depth: int = 0) -> Any:
    if isinstance(value, str):
        text = _scrub(value, secrets)
        return text if len(text) <= _MAX_TEXT else f"{text[:_MAX_TEXT]}…"
    if depth >= 3:
        return value
    if isinstance(value, dict):
        return {str(k): _clean(v, secrets, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v, secrets, depth + 1) for v in value[:20]]
    return value


def sanitize_event(event: dict[str, Any], secrets: list[str] | None = None) -> dict[str, Any] | None:
    """One bus event as it may be written to disk, or `None` when it must not be persisted at all."""
    name = str(event.get("event") or "")
    if name in _DROP_EVENTS:
        return None
    if name == EVENT_VOICE_STT and event.get("room") is True:
        return None
    secrets = _secret_values() if secrets is None else secrets
    out: dict[str, Any] = {}
    for key, value in event.items():
        if name == EVENT_PAIR_REQUESTED and key == "code":
            out[key] = "[redacted]"
            continue
        out[str(key)] = _clean(value, secrets)
    return out


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _day_after(name: str) -> str:
    """The exclusive upper bound for one day folder, so pruning removes that day's rows too."""
    day = datetime.strptime(name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (day + timedelta(days=1)).strftime("%Y-%m-%d")


def _local_date() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


class TraceSink:
    """Drains `TelemetryHub` into daily JSON, `trace_runs` rows and the friction ledger."""

    def __init__(self, db: BrainDB, cfg: Settings, hub: TelemetryHub) -> None:
        self.db = db
        self.cfg = cfg
        self.hub = hub
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._buffers: dict[str, dict[str, Any]] = {}
        self._last_by_session: dict[str, dict[str, Any]] = {}
        self._recent: deque[dict[str, Any]] = deque(maxlen=20)
        self._prompt_sha_cache: tuple[float, str] | None = None
        self._git_sha = ""
        self._dropped = 0

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._queue = asyncio.Queue(maxsize=4096)
        self.hub.add_listener(self._on_event)
        removed = self.prune()
        if removed:
            logger.info(f"[Traces] pruned {removed} day folder(s) older than {self.cfg.trace_retention_days}d")
        self._task = asyncio.create_task(self._run(), name="trace-sink")

    async def stop(self) -> None:
        self.hub.remove_listener(self._on_event)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for buffer in list(self._buffers.values()):
            self._flush(buffer, force=True)
        self._buffers.clear()

    def _on_event(self, envelope: dict[str, Any]) -> None:
        queue = self._queue
        if queue is None:
            return
        try:
            queue.put_nowait(envelope)
        except asyncio.QueueFull:
            self._dropped += 1
            try:
                queue.get_nowait()  # keep the newest: a slow disk must not wedge the bus
                queue.put_nowait(envelope)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    # ------------------------------------------------------------------ writer

    async def _run(self) -> None:
        last_sweep = time.monotonic()
        while True:
            assert self._queue is not None
            first = await self._queue.get()
            batch = [first]
            while len(batch) < 200:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self._drain(batch)
            except Exception as exc:  # noqa: BLE001 - tracing must never kill the daemon
                logger.warning(f"[Traces] drain failed: {exc!r}")
            if time.monotonic() - last_sweep > 60:
                last_sweep = time.monotonic()
                self._sweep()

    async def _drain(self, batch: list[dict[str, Any]]) -> None:
        secrets = _secret_values()
        lines: dict[str, list[str]] = {}
        ready: list[dict[str, Any]] = []
        for raw in batch:
            event = sanitize_event(raw, secrets)
            if event is None:
                continue
            trace_id = str(event.get("trace_id") or "")
            if not trace_id:
                continue
            event["trace_id"] = trace_id
            day = _day(float(event.get("ts") or time.time()))
            lines.setdefault(day, []).append(json.dumps(event, separators=(",", ":"), ensure_ascii=False))
            buffer = self._buffers.get(trace_id)
            if buffer is None:
                buffer = self._new_buffer(trace_id, float(event.get("ts") or time.time()))
                self._buffers[trace_id] = buffer
            self._ingest(buffer, event)
            buffer["touched"] = time.time()
            if self._closed(buffer):
                ready.append(buffer)
        if lines:
            await asyncio.to_thread(self._append_spans, lines)
        for buffer in ready:
            self._buffers.pop(buffer["trace_id"], None)
            await asyncio.to_thread(self._flush, buffer, False)

    @staticmethod
    def _new_buffer(trace_id: str, ts: float) -> dict[str, Any]:
        return {
            "trace_id": trace_id,
            "turn_id": "",
            "session_id": "",
            "source": "voice",
            "started": ts,
            "finished": ts,
            "voice": {},
            "brain": {},
            "run_id": None,
            "has_run": False,
            "voice_closed": False,
            "run_closed": False,
            "outcome": "ok",
            "friction": None,
            "surface": "",
            "causal": "",
            "user_input": "",
            "summary": "",
            "error": "",
            "latency_ms": None,
            "cost_usd": 0.0,
            "task_id": None,
            "attempt_id": None,
            "skills_used": [],
            "superseded_by": None,
            "touched": time.time(),
        }

    @staticmethod
    def _closed(buffer: dict[str, Any]) -> bool:
        if buffer["has_run"]:
            return buffer["run_closed"]
        return buffer["voice_closed"]

    def _ingest(self, b: dict[str, Any], event: dict[str, Any]) -> None:
        name = str(event.get("event") or "")
        ts = float(event.get("ts") or time.time())
        b["finished"] = max(b["finished"], ts)
        if not b["turn_id"]:
            b["turn_id"] = str(event.get("turn_id") or "")
        if not b["session_id"]:
            b["session_id"] = str(event.get("session_id") or "")
        voice = b["voice"]
        if name == EVENT_VOICE_STT:
            voice["stt"] = str(event.get("text") or "")
            voice["speaker"] = str(event.get("speaker") or "")
            b["user_input"] = b["user_input"] or voice["stt"]
        elif name == EVENT_VOICE_SPEAKER:
            voice["speaker"] = str(event.get("label") or voice.get("speaker") or "")
        elif name == EVENT_VOICE_INTENT:
            voice["intent"] = str(event.get("intent") or "")
            voice["intent_ms"] = event.get("ms")
        elif name == EVENT_VOICE_CLEANUP:
            voice["cleanup"] = str(event.get("text") or "")
        elif name == EVENT_VOICE_MEMORY:
            voice["memory_hits"] = event.get("hits")
        elif name == EVENT_VOICE_REQUEST:
            voice["messages"] = event.get("messages")
        elif name == EVENT_VOICE_TOOL:
            voice["tool"] = str(event.get("name") or event.get("tool") or "")
        elif name == EVENT_VOICE_DISPATCH:
            b["has_run"] = True
            b["run_id"] = b["run_id"] or str(event.get("run_id") or "")
            b["user_input"] = b["user_input"] or str(event.get("text") or "")
        elif name == EVENT_VOICE_RETURN:
            b["summary"] = str(event.get("text") or b["summary"])
            if event.get("outcome") == "failed":
                b["outcome"] = "errored"
                b["error"] = str(event.get("text") or "")
        elif name == EVENT_VOICE_RETELL:
            voice["retell"] = str(event.get("text") or "")
        elif name == EVENT_VOICE_LATENCY:
            voice["latency"] = str(event.get("text") or "")
        elif name == "voice.feedback":
            # A speech-check correction inside the window: back-patch the turn it refers to.
            self.mark_feedback(str(event.get("signal") or ""))
        elif name == EVENT_VOICE_TTS:
            b["voice_closed"] = True
            b["latency_ms"] = int(max(0.0, (b["finished"] - b["started"]) * 1000))
            b["summary"] = b["summary"] or str(event.get("text") or "")
        elif name == EVENT_RUN_STATUS:
            status = str(event.get("status") or "")
            if status == "running":
                b["has_run"] = True
                b["run_id"] = b["run_id"] or str(event.get("run_id") or "")
            else:
                b["run_closed"] = True
                b["outcome"] = {
                    "completed": "ok",
                    "failed": "errored",
                    "cancelled": "cancelled",
                }.get(status, b["outcome"])
                b["error"] = b["error"] or str(event.get("error") or "")
        elif name == EVENT_HTTP_COMPLETED:
            b["run_closed"] = True
            b["summary"] = b["summary"] or str(event.get("spoken") or "")
        elif name == EVENT_BRAIN_ROUTE:
            phase = str(event.get("phase") or "")
            if phase == "route":
                b["brain"]["choice"] = str(event.get("choice") or "")
                b["brain"]["runner"] = str(event.get("runner") or "")
                b["brain"]["project"] = str(event.get("project") or "")
                b["brain"]["route_ms"] = event.get("route_ms")
                b["has_run"] = True
            elif phase == "done":
                b["brain"]["outcome"] = str(event.get("outcome") or "")
                b["summary"] = b["summary"] or str(event.get("summary") or "")
        elif name == EVENT_ATTEMPT_FINISHED:
            b["task_id"] = str(event.get("task_id") or "")
            b["attempt_id"] = event.get("attempt_id")
            b["cost_usd"] = float(event.get("cost_usd") or 0.0)
            b["outcome"] = {
                "pushed": "ok",
                "decomposed": "ok",
                "failed_verify": "failed_verify",
                "timed_out": "timed_out",
                "errored": "errored",
                "deferred_approval": "ok",
                "reaped": "cancelled",
            }.get(str(event.get("outcome") or ""), b["outcome"])

    # ------------------------------------------------------------------ friction

    def note_run_created(
        self,
        *,
        trace_id: str,
        turn_id: str,
        session_id: str,
        user_input: str,
        project: str,
        runner: str,
        replace: bool,
    ) -> None:
        """Record the run for session-scoped override detection; a correction is handled by `mark_replaced`."""
        previous = self._last_by_session.get(session_id)
        if previous and not replace and time.time() - previous["at"] <= 120:
            old = self._words(previous["text"])
            new = self._words(user_input)
            overlap = len(old & new) / len(old) if old else 0.0
            added_target = bool(project and project != previous["project"]) or (
                runner and runner != previous["runner"]
            )
            if overlap >= 0.5 and added_target and previous["trace_id"] != trace_id:
                self.db.mark_trace_friction(
                    previous["trace_id"],
                    friction_kind="router_override",
                    surface="brain_router",
                    causal_sig=causal_signature(
                        "wrong_target", previous["runner"] or "unknown", "router_disambiguation"
                    ),
                    outcome="friction",
                    superseded_by=trace_id,
                )
        self._last_by_session[session_id] = {
            "trace_id": trace_id,
            "turn_id": turn_id,
            "text": user_input,
            "project": project,
            "runner": runner,
            "at": time.time(),
        }

    def mark_replaced(
        self,
        old_trace_id: str,
        new_trace_id: str,
        *,
        old_project: str = "",
        new_project: str = "",
        old_runner: str = "",
        new_runner: str = "",
    ) -> None:
        changed_target = bool(
            (new_project and new_project != old_project) or (new_runner and new_runner != old_runner)
        )
        surface = "brain_router" if changed_target else "voice_prompts"
        self.db.mark_trace_friction(
            old_trace_id,
            friction_kind="replaced",
            surface=surface,
            causal_sig=causal_signature("user_corrected", old_runner or "unknown", "misread_task_or_target"),
            outcome="friction",
            superseded_by=new_trace_id,
        )
        self._buffer_friction(old_trace_id, "replaced", surface, "user_corrected|misread|task")

    def mark_stopped(self, trace_id: str) -> None:
        self.db.mark_trace_friction(
            trace_id,
            friction_kind="stopped",
            surface="builder_harness",
            causal_sig=causal_signature("cancelled", "user_abort", "wrong_or_slow_execution"),
            outcome="cancelled",
        )
        self._buffer_friction(trace_id, "stopped", "builder_harness", "cancelled|user_abort|execution")

    def mark_feedback(self, signal: str) -> None:
        """A speech-check correction points back at the turn that was misheard (or misclassified)."""
        kind = {"not you": "not_you", "said again": "repeated"}.get(signal.strip().lower(), "undo_mute")
        now = time.time()
        for entry in reversed(self._recent):
            if now - entry["at"] > 45:
                break
            if entry["source"] != "voice":
                continue
            self.db.mark_trace_friction(
                entry["trace_id"],
                friction_kind=kind,
                surface="voice_intent_data",
                causal_sig=causal_signature(f"speech_check_{kind}", entry["surface"] or "intent", "intent_model"),
                outcome="friction",
            )
            return

    def record_attempt(
        self,
        *,
        attempt_id: int,
        task_id: str,
        outcome: str,
        summary: str,
        cost_usd: float,
        git_sha: str = "",
        skills_used: tuple[str, ...] = (),
        repeated_error_tools: tuple[str, ...] = (),
    ) -> None:
        surface, causal = classify_surface(
            outcome, repeated_error_tools=repeated_error_tools, used_skills=bool(skills_used)
        )
        trace_id = f"attempt-{attempt_id}"
        day = datetime.now().astimezone().strftime("%Y-%m-%d")
        self.db.upsert_trace_run(
            TraceRunRow(
                trace_id=trace_id,
                turn_id="",
                source="board",
                surface=surface or "builder_harness",
                runner="pi",
                repo="",
                task_id=task_id,
                attempt_id=attempt_id,
                outcome="ok" if outcome in ("pushed", "decomposed") else outcome,
                friction_kind="" if outcome in ("pushed", "decomposed") else f"attempt_{outcome}",
                causal_sig=causal,
                latency_ms=None,
                ttfb_ms=None,
                tokens_in=0,
                tokens_out=0,
                cost_usd=cost_usd,
                git_sha=git_sha,
                prompt_sha=self.prompt_sha(),
                skills_used=skills_used,
                user_input=summary[:600],
                output_summary=summary[:_SUMMARY_CHARS],
                trace_path=f"{day}/attempt-{attempt_id}.json",
                superseded_by=None,
                reflected=0,
            )
        )

    def _buffer_friction(self, trace_id: str, kind: str, surface: str, causal: str) -> None:
        buffer = self._buffers.get(trace_id)
        if buffer is not None:
            buffer["friction"] = kind
            buffer["surface"] = surface
            buffer["causal"] = causal

    @staticmethod
    def _words(text: str) -> set[str]:
        return set(re.findall(r"\w+", text.lower()))

    # ------------------------------------------------------------------ persist

    def _flush(self, buffer: dict[str, Any], force: bool) -> None:
        if not force and not self._closed(buffer):
            return
        b = buffer
        outcome = str(b["outcome"] or "ok")
        friction = b["friction"]
        surface = b["surface"]
        causal = b["causal"]
        voice = b["voice"]
        if not friction and voice.get("stt") and voice.get("cleanup"):
            ratio = word_change_ratio(str(voice["stt"]), str(voice["cleanup"]))
            if ratio >= 0.35:
                friction = "asr_rewrite"
                surface = "whisper_asr_data"
                causal = causal_signature("cleanup_rewrote", f"ratio={ratio:.2f}", "asr_domain_terms")
        latency = b["latency_ms"]
        if not friction and outcome == "ok" and latency is not None and latency > 2500:
            route_ms = b["brain"].get("route_ms")
            slow_route = isinstance(route_ms, (int, float)) and route_ms > 800
            friction = "latency_sla"
            surface = "brain_router" if slow_route else "voice_prompts"
            causal = causal_signature("slow_turn", f"{latency}ms", "latency_budget")
        if not surface and outcome != "ok":
            surface, derived = classify_surface(outcome)
            causal = causal or derived

        day = _day(b["started"])
        trace_path = f"{day}/trace-{b['trace_id']}.json"
        record = {
            "trace_id": b["trace_id"],
            "turn_id": b["turn_id"],
            "source": b["source"],
            "started_at": datetime.fromtimestamp(b["started"]).isoformat(timespec="seconds"),
            "started_ts": round(float(b["started"]), 3),
            "duration_ms": int(max(0.0, (b["finished"] - b["started"]) * 1000)),
            "git_sha": self.git_sha(),
            "prompt_sha": self.prompt_sha(),
            "voice": voice,
            "brain": b["brain"],
            "run_id": b["run_id"],
            "task_id": b["task_id"],
            "attempt_id": b["attempt_id"],
            "user_input": b["user_input"][:2000],
            "summary": b["summary"][:2000],
            "error": b["error"][:1000],
            "outcome": outcome,
            "friction_kind": friction,
            "surface": surface,
            "causal_sig": causal,
            "cost_usd": round(float(b["cost_usd"]), 4),
        }
        self._write_trace_file(trace_path, record)
        self.db.upsert_trace_run(
            TraceRunRow(
                trace_id=b["trace_id"],
                turn_id=b["turn_id"],
                source=b["source"],
                surface=surface or "voice_prompts",
                runner=str(b["brain"].get("runner") or ""),
                repo=str(b["brain"].get("project") or ""),
                task_id=b["task_id"],
                attempt_id=b["attempt_id"],
                outcome=outcome,
                friction_kind=friction,
                causal_sig=causal,
                latency_ms=latency,
                ttfb_ms=None,
                tokens_in=0,
                tokens_out=0,
                cost_usd=round(float(b["cost_usd"]), 4),
                git_sha=self.git_sha(),
                prompt_sha=self.prompt_sha(),
                skills_used=tuple(b["skills_used"]),
                user_input=b["user_input"][:600],
                output_summary=b["summary"][:_SUMMARY_CHARS],
                trace_path=trace_path,
                superseded_by=b["superseded_by"],
                reflected=0,
            )
        )
        self._recent.append(
            {
                "trace_id": b["trace_id"],
                "at": time.time(),
                "source": b["source"],
                "surface": surface,
            }
        )
        self._maybe_export(record)

    def _write_trace_file(self, relative: str, record: dict[str, Any]) -> None:
        path = self.cfg.traces_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        existing: dict[str, Any] = {}
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = {}
        merged = {**existing, **record}
        path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
        os.chmod(path, 0o600)

    def _append_spans(self, lines: dict[str, list[str]]) -> None:
        for day, batch in lines.items():
            folder = self.cfg.traces_dir / day
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = folder / "spans.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(batch) + "\n")
            os.chmod(path, 0o600)

    def _maybe_export(self, record: dict[str, Any]) -> None:
        """Optional OTLP/HTTP JSON push; zero cost and zero imports when no endpoint is configured."""
        endpoint = (self.cfg.otel_exporter_otlp_endpoint or "").strip()
        if not endpoint:
            return
        payload = {
            "resourceSpans": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "jarvis"}}]},
                    "scopeSpans": [
                        {
                            "scope": {"name": "jarvis.traces"},
                            "spans": [
                                {
                                    "traceId": record["trace_id"],
                                    "name": f"turn {record.get('source', 'voice')}",
                                    "startTimeUnixNano": str(int(float(record.get("started_ts") or 0) * 1e9)),
                                    "attributes": [
                                        {"key": "gen_ai.operation.name", "value": {"stringValue": "invoke_agent"}},
                                        {"key": "gen_ai.agent.name", "value": {"stringValue": "jarvis"}},
                                        {"key": "jarvis.surface", "value": {"stringValue": record.get("surface") or ""}},
                                        {"key": "jarvis.outcome", "value": {"stringValue": record.get("outcome") or ""}},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        asyncio.create_task(self._post_otlp(endpoint, payload))

    @staticmethod
    async def _post_otlp(endpoint: str, payload: dict[str, Any]) -> None:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                await client.post(endpoint, json=payload)
        except Exception as exc:  # noqa: BLE001 - an optional mirror must never fail a turn
            logger.debug(f"[Traces] OTLP export skipped ({exc!r})")

    # ------------------------------------------------------------------ housekeeping

    def _sweep(self) -> None:
        cutoff = time.time() - _FLUSH_AFTER_SECS
        for trace_id, buffer in list(self._buffers.items()):
            if float(buffer["touched"]) < cutoff:
                self._buffers.pop(trace_id, None)
                try:
                    self._flush(buffer, force=True)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[Traces] stale flush failed for {trace_id}: {exc!r}")

    def prune(self) -> int:
        """Delete day folders past retention, then oldest-first while the folder is over its size cap."""
        root = self.cfg.traces_dir
        if not root.is_dir():
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, self.cfg.trace_retention_days))
        removed = 0
        days: list[Path] = []
        for child in sorted(root.iterdir()):
            if not child.is_dir() or not _DAY_RE.match(child.name):
                continue
            try:
                day = datetime.strptime(child.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if day < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                self.db.delete_traces_before(_day_after(child.name))
                removed += 1
            else:
                days.append(child)
        cap = max(1, self.cfg.trace_max_mb) * 1024 * 1024
        total = sum(f.stat().st_size for folder in days for f in folder.rglob("*") if f.is_file())
        for folder in days:
            if total <= cap:
                break
            size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
            shutil.rmtree(folder, ignore_errors=True)
            self.db.delete_traces_before(_day_after(folder.name))
            total -= size
            removed += 1
        return removed

    def git_sha(self) -> str:
        if not self._git_sha:
            try:
                self._git_sha = git_version()
            except Exception:  # noqa: BLE001 - a version string is a nice-to-have on a trace
                self._git_sha = "unknown"
        return self._git_sha

    def prompt_sha(self) -> str:
        """Hash the shipped prompts plus the user's `SYSTEM.md`, so a prompt edit is visible on every run."""
        from jarvis_core.home import JARVIS_HOME, REPO_ROOT

        parts = [REPO_ROOT / "apps" / "voice" / "src" / "jarvis_voice" / "prompts.py", JARVIS_HOME / "SYSTEM.md"]
        newest = 0.0
        for path in parts:
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
        if self._prompt_sha_cache is not None and self._prompt_sha_cache[0] == newest:
            return self._prompt_sha_cache[1]
        digest = hashlib.sha256()
        for path in parts:
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"-")
        sha = digest.hexdigest()[:16]
        self._prompt_sha_cache = (newest, sha)
        return sha


# ---------------------------------------------------------------------- distillation


def distill_experience(db: BrainDB, cfg: Settings, *, days: int = 7) -> Path:
    """Write the layered digest the offline Improver reads first: overview, then one file per surface."""
    analysis = cfg.traces_dir / "analysis"
    surfaces_dir = analysis / "surfaces"
    surfaces_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    rows = db.list_trace_runs(limit=800)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = [
        r
        for r in rows
        if r.created_at >= cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    ]
    stats = db.trace_sha_stats()
    clusters: dict[tuple[str, str], list[TraceRunRow]] = {}
    for row in recent:
        if row.outcome == "ok" and not row.friction_kind:
            continue
        key = (row.surface or "unknown", row.causal_sig or f"{row.outcome}|unknown|unknown")
        clusters.setdefault(key, []).append(row)
    ordered = sorted(clusters.items(), key=lambda item: len(item[1]), reverse=True)

    lines = [
        "# Trace digest",
        "",
        f"Window: last {days} days, {len(recent)} run(s) of {len(rows)} kept.",
        "",
        "## Health by commit",
    ]
    if stats:
        for stat in stats[:6]:
            lines.append(
                f"- {stat['git_sha'][:24]} runs={stat['runs']} ok={stat['ok']} "
                f"friction={stat['friction']} failed={stat['failed']} "
                f"p50_latency_ms={stat['p50_latency_ms']} cost=${stat['cost_usd']:.2f}"
            )
    else:
        lines.append("- (no runs recorded yet)")
    lines.extend(["", "## Top failure and friction clusters"])
    if ordered:
        for (surface, sig), items in ordered[:12]:
            lines.append(f"- {surface} x{len(items)}: {sig}")
            for row in items[-2:]:
                lines.append(f"    {row.trace_id} {row.outcome} → {row.trace_path}")
    else:
        lines.append("- (none)")
    pairs = _partial_pairs(recent)
    lines.extend(["", "## Failed-then-passed pairs (read both before proposing a fix)"])
    if pairs:
        for failed, passed in pairs[:8]:
            lines.append(f"- {failed.trace_id} ({failed.surface}) → {passed.trace_id} ({passed.trace_path})")
    else:
        lines.append("- (none)")
    lines.extend(["", "## Surfaces", "Read `surfaces/<id>.md` before editing that component."])
    for surface, info in sorted(HARNESS_SURFACES.items()):
        count = sum(1 for row in recent if row.surface == surface)
        gate = f" gate: `{info.gate_cmd}`" if info.gate_cmd else ""
        lines.append(f"- {surface} ({info.kind}) runs={count}{gate}")

    overview = analysis / "overview.md"
    overview.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.chmod(overview, 0o600)

    for surface, info in HARNESS_SURFACES.items():
        rows_for = [row for row in recent if row.surface == surface]
        body = [f"# {surface} ({info.kind})", ""]
        body.append(f"Editable: {', '.join(info.editable_files)}")
        if info.gate_cmd:
            body.append(f"Gate: `{info.gate_cmd}`")
        body.append("")
        if not rows_for:
            body.append("(no runs in the window)")
        for row in rows_for[-15:]:
            body.append(
                f"- {row.created_at} {row.trace_id} [{row.outcome}/{row.friction_kind or 'ok'}] "
                f"sig={row.causal_sig or '(none)'} trace={row.trace_path}"
            )
            if row.user_input:
                body.append(f"    in: {row.user_input[:160]}")
            if row.output_summary:
                body.append(f"    out: {row.output_summary[:160]}")
        path = surfaces_dir / f"{surface}.md"
        path.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    return overview


def _partial_pairs(rows: list[TraceRunRow]) -> list[tuple[TraceRunRow, TraceRunRow]]:
    """A failed/friction run and the later run that replaced it, so the proposer can diff the retry."""
    by_id = {row.trace_id: row for row in rows}
    pairs: list[tuple[TraceRunRow, TraceRunRow]] = []
    for row in rows:
        if not row.superseded_by:
            continue
        later = by_id.get(row.superseded_by)
        if later is not None and later.outcome == "ok":
            pairs.append((row, later))
    return pairs


__all__ = [
    "HARNESS_SURFACES",
    "HarnessSurface",
    "TraceSink",
    "causal_signature",
    "classify_surface",
    "distill_experience",
    "sanitize_event",
    "word_change_ratio",
]
