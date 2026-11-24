# Phase 1 Implementation Plan: Full-Stack Harness Observability (plan, 2026-11-24)

Status: **implemented** (`6a77d89` + traces). Commit-sized slices landed together: correlation + `TraceSink`, `trace_runs`/FTS + friction, distillation + nightly ASSIST reflection, `jarvis traces` + cockpit cold start + optional OTLP.
Companions: `docs/research/observability-landscape.md` (why this design won) and `docs/self-improvement-plan.md`
(how Phase 1 feeds Phases 2–5).

Implementation notes: the cockpit's cold start synthesises `STT`/`ROUTE`/`RETURN` from a `trace_runs` row instead
of parsing 40 trace JSON files on every snapshot poll; `trace_fts` is a plain FTS5 table refreshed on upsert; the
nightly check is `nightly_reflect_hour` (default 3) rather than a cron string.

---

## 0. Design Decisions & Non-Negotiables

1. **Zero new daemons, zero Docker, zero new Python/Rust dependencies.** Everything runs inside the single
   `jarvis` process using Python 3.12's built-in SQLite 3.53 (`ENABLE_FTS5`), `httpx`, `TelemetryHub`, and
   `apps/tui`'s bundled `rusqlite`.
2. **Hot path stays 100% non-blocking (`0 ms` added to voice/TTS).** `TelemetryHub.emit()` remains synchronous
   and memory-only; persistence runs in an async background consumer (`TraceSink`) off a bounded drop-oldest
   queue (`maxsize=4096`). A slow or full disk never stalls audio or a worker.
3. **Hard security & privacy redaction before disk:**
   - Never persist `pair.requested` `code` digits (`server.py:595` — pairing codes belong on live `jarvis top`
     or `jarvis pair` stdout only).
   - Drop high-frequency/keepalive noise (`voice.level`, `heartbeat`).
   - Scrub credential values (`SECRET_ENV` values from `sandbox.py` + `Bearer ` / `sk-` / API key patterns).
   - Never persist unaddressed `[Room]` speech in meeting mode (`voice_gates.py`).
4. **Tracer and trace store are read-only to workers.** `apps/brain/src/brain/traces.py` is added to
   `PROTECTED_SELF_PATHS` (`reconciler.py:72`). `sandbox.py` continues to deny `traces/` and `state.db*` to all
   workers except `Improver` (`allow_read_traces=True`, write still denied), preventing reward hacking.
5. **OpenTelemetry `gen_ai.*` span vocabulary + optional OTLP HTTP push.** Local JSONL spans use standard OTel
   GenAI attribute names so setting `OTEL_EXPORTER_OTLP_ENDPOINT` can mirror traces to `victoria-traces` or
   `uvx arize-phoenix serve` on demand with zero lock-in.

---

## 1. File Inventory

| Action | Path | Est. LOC | Purpose |
| :--- | :--- | :---: | :--- |
| **New** | `apps/brain/src/brain/traces.py` | ~380 | `HARNESS_SURFACES`, `TraceSink` (hub subscriber, redaction, daily JSONL writer, retention, optional OTLP HTTP push), `ImplicitFrictionDetector`, causal $\phi(r)$ classifier, and `distill_experience()` (`traces/analysis/*.md`). |
| **New** | `apps/brain/tests/test_traces.py` | ~240 | Redaction invariants, end-to-end `trace_id`/`turn_id` correlation, all 8 implicit friction transitions, FTS5 search, daily retention pruning, `overview.md` distillation, and mock OTLP export. |
| **Modify** | `apps/voice/src/jarvis_voice/telemetry.py` | ~25 | Mint globally unique 16-byte hex `trace_id` alongside session-local `turn_id` (`t1`, `t2`, …); expose `current_trace()`. |
| **Modify** | `apps/voice/src/jarvis_voice/agent_tools.py` | ~12 | Pass `replace=bool(args.get("replace"))` through to `agent.send_task(...)` instead of swallowing the replacement reason locally. |
| **Modify** | `apps/voice/src/jarvis_voice/brain_client.py` | ~20 | Send `turn_id`, `trace_id`, and `replace` in `POST /v1/runs` (and `X-Jarvis-Trace-Id` on `/answer`, `/stop`), fixing the missing `ROUTE` stage in live `jarvis-top` X-Ray. |
| **Modify** | `apps/voice/src/jarvis_voice/feedback.py` | ~10 | Emit `voice.feedback` on `telemetry` whenever `_correct()` records an implicit speech-check correction (`not you`, `said again`, undone mute) so `TraceSink` back-patches the offending turn in `state.db`. |
| **Modify** | `apps/brain/src/brain/config.py` | ~12 | Add `trace_retention_days: int = 30`, `trace_max_mb: int = 500`, `nightly_reflect_cron: str = "0 3 * * *"`, `otel_exporter_otlp_endpoint: str = ""`. |
| **Modify** | `apps/brain/src/brain/schema.sql` & `db.py` | ~130 | Add `trace_runs` table + `trace_fts` FTS5 virtual table + CRUD/query/SHA-diff helpers (`upsert_trace_run`, `mark_trace_friction`, `search_traces`, `compare_git_shas`, `list_unreflected_traces`). |
| **Modify** | `apps/brain/src/brain/server.py` & `orchestrator.py` | ~65 | Thread `turn_id` + `trace_id` into `RunRecord` and `EVENT_BRAIN_ROUTE`; start/stop `TraceSink` in `BrainServer`; add `GET /v1/traces` and `GET /v1/traces/diff` operator routes; update `schemas/openapi.json`. |
| **Modify** | `apps/brain/src/brain/skills.py` & `reconciler.py` | ~80 | Protect `traces.py` in `PROTECTED_SELF_PATHS`; record board attempts into `trace_runs`; replace 15-line `_surface_for_trace` with `distill_experience()`; seed idempotent nightly `03:00` reflection check that runs even when `mode == 'ASSIST'`. |
| **Modify** | `src/jarvis/cli.py` | ~60 | Add `jarvis traces` (`--failed`, `--friction`, `--surface`, `--search`, `--diff <sha1>..<sha2>`, `--id <trace_id>`). |
| **Modify** | `apps/tui/src/{db.rs,model.rs,app.rs,ui/xray.rs}` | ~95 | Hydrate the last 50 turns into `app.traces` on cold start from `state.db` (`trace_runs`) + `traces/YYYY-MM-DD/trace-<id>.json` so restarts and late `jarvis top` launches show full Turn X-Ray history. |

---

## 2. Detailed Subsystem Specification

### Step 1: End-to-End Correlation (`turn_id` + `trace_id`) & Live Bug Fixes

**Problem:** Today `jarvis-voice` stamps `turn_id="t1"` (`apps/voice/src/jarvis_voice/telemetry.py:36`), which
resets to `"t1"` on every restart and is never sent to `brain`. As a result, `BrainOrchestrator.route` emits
`brain.route` with no `turn_id`, dropping `ROUTE` from `jarvis-top` Pane 5 (`apps/tui/src/app.rs:852-873`), and
restarts collide IDs on disk.

**Changes:**
1. In `apps/voice/src/jarvis_voice/telemetry.py`:
   - Keep `_current_turn = f"t{next(_turn_ids)}"` untouched so `apps/tui`'s existing `turn_id` tests and live
     display stay 100% compatible.
   - Add `_current_trace = uuid.uuid4().hex` (32-char OTel-valid 128-bit trace ID) minted in `new_turn()`, and
     include `trace_id=_current_trace` in every `emit()` envelope.
2. In `apps/voice/src/jarvis_voice/agent_tools.py` & `brain_client.py`:
   - Change `send_task(self, task: str, *, replace: bool = False)` to include `"replace": True` when `replace`
     is set (keep the fast local `await agent.interrupt()` if needed, or let `server.py:784` atomically stop and
     link `replaced_trace_id`), plus `"turn_id": telemetry.current_turn()` and `"trace_id": telemetry.current_trace()`.
   - Send header `X-Jarvis-Trace-Id: <trace_id>` on `/answer` and `/stop`.
3. In `apps/brain/src/brain/server.py` & `orchestrator.py`:
   - Add `turn_id: str = ""` and `trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)` to `RunRecord`.
   - Pass `turn_id=rec.turn_id, trace_id=rec.trace_id` into `BrainOrchestrator.route(...)` and include both in
     every `EVENT_BRAIN_ROUTE`, `EVENT_RUN_STATUS`, and `EVENT_HTTP_COMPLETED` emission.

---

### Step 2: `HARNESS_SURFACES` Manifest (Component Observability)

Declare Jarvis's editable action space explicitly in `apps/brain/src/brain/traces.py` so every failure or
friction event maps to **one** concrete component and its benchmark gate (AHE Pillar 1):

```python
@dataclass(frozen=True)
class HarnessSurface:
    id: str
    kind: Literal["prompt", "router", "tool", "skill", "memory", "weights"]
    editable_files: tuple[str, ...]
    gate_cmd: str | None          # mandatory benchmark before any edit can land

HARNESS_SURFACES: dict[str, HarnessSurface] = {
    "voice_intent_data": HarnessSurface(
        id="voice_intent_data",
        kind="weights",
        editable_files=("apps/voice/src/jarvis_voice/executive_filter.py",),
        gate_cmd="uv run python scripts/bench_intent.py 1",
    ),
    "whisper_asr_data": HarnessSurface(
        id="whisper_asr_data",
        kind="weights",
        editable_files=("apps/voice/src/jarvis_voice/cleanup.py", "SYSTEM.md"),
        gate_cmd="uv run pytest apps/voice/tests",
    ),
    "voice_prompts": HarnessSurface(
        id="voice_prompts",
        kind="prompt",
        editable_files=(
            "apps/voice/src/jarvis_voice/prompts.py",
            "apps/voice/src/jarvis_voice/agent_tools.py",
            "apps/voice/src/jarvis_voice/languages.json",
            "SYSTEM.md",
        ),
        gate_cmd="uv run python scripts/bench_routing.py 1",
    ),
    "brain_router": HarnessSurface(
        id="brain_router",
        kind="router",
        editable_files=("apps/brain/src/brain/orchestrator.py",),
        gate_cmd="uv run pytest apps/brain/tests/test_orchestrator.py",  # upgraded to bench_brain_route.py in Phase 2
    ),
    "builder_harness": HarnessSurface(
        id="builder_harness",
        kind="tool",
        editable_files=("apps/brain/src/brain/reconciler.py",),
        gate_cmd="uv run pytest apps/brain/tests",
    ),
    "skills": HarnessSurface(
        id="skills",
        kind="skill",
        editable_files=("skills/*/SKILL.md",),
        gate_cmd=None,  # gated by validate_skill_proposal + 2-failure auto-rollback
    ),
    "memory": HarnessSurface(
        id="memory",
        kind="memory",
        editable_files=("memory/MEMORY.md", "memory/repos/*.md"),
        gate_cmd=None,  # gated by validate_memory_text + expected_sha256
    ),
}
```

---

### Step 3: `TraceSink` & Daily Partitioned OTel-JSONL (`Layer 1`)

**Directory layout under `JARVIS_HOME/brain/traces/` (`0700` dirs, `0600` files):**

```text
JARVIS_HOME/brain/traces/
├── 2026-11-24/
│   ├── spans.jsonl                # append-only OTel gen_ai.* spans across the day
│   ├── trace-9f81c2...a01.json    # complete stitched turn/run artifact (written on close)
│   └── ...
├── analysis/                      # regenerated before each Improver run (Step 5)
│   ├── overview.md                # <= 120-line cross-surface digest + git_sha deltas
│   └── surfaces/
│       ├── brain_router.md
│       └── builder_harness.md
└── attempt-<id>.json              # kept as hardlink/alias or legacy compatibility during transition
```

**`TraceSink` mechanics (`apps/brain/src/brain/traces.py`):**
1. Registers a non-blocking listener hook on `TelemetryHub.emit` (a `Callable[[dict[str, Any]], None]` list on
   `TelemetryHub` alongside `_subscribers`, pushing to `asyncio.Queue(maxsize=4096)` and dropping if full).
2. **Redaction filter (`sanitize_event`):**
   - Drops `voice.level` and `heartbeat`.
   - On `pair.requested`, strips `code` (`{"event": "pair.requested", "code": "[REDACTED]", ...}`).
   - Replaces any literal occurrence of non-empty `SECRET_ENV` values plus regex matches for
     `Bearer [A-Za-z0-9._-]+` and `sk-[A-Za-z0-9_-]{16,}` with `[REDACTED]`.
   - Drops `voice.stt` / `voice.speaker` events where `room == True` and intent is `IGNORE_BACKGROUND`.
3. **In-flight turn/run stitcher:**
   - Accumulates stage events keyed by `trace_id` with a 30-minute idle eviction timer (for long T3 runs; flush
     immediately on `voice.tts` when no agent run was dispatched, or on `run.completed`/`run.failed`/`run.cancelled`/`attempt.finished`).
   - Writes one normalized `trace-<trace_id>.json` containing:
     - `trace_id`, `turn_id`, `source`, `started_at`, `duration_ms`, `git_sha`, `prompt_sha`
     - `voice`: `{stt, speaker, intent, laya, jev, cleanup, memory_hits, tool_call, retell, latency}`
     - `brain`: `{run_id, route_choice, runner, project, outcome, summary, error, cost_usd, tokens_in, tokens_out}`
     - `worker`: `{task_id, attempt_id, role, skills_used, tool_calls, tool_errors, oracle, verify_output, diff_stat}`
     - `attribution`: `{surface, outcome, friction_kind, causal_sig}`
4. **Retention sweep (`prune_old_traces`):**
   - Runs once at `TraceSink.start()` and once per day: deletes `YYYY-MM-DD/` directories older than
     `cfg.trace_retention_days` (default `30`), then oldest-first if total size under `traces/` exceeds
     `cfg.trace_max_mb` (default `500 MB`), and deletes matching `trace_runs` / `trace_fts` rows in `state.db`.

---

### Step 4: SQLite `trace_runs` + `trace_fts` & 8-Signal Implicit Friction (`Layer 2`)

**Schema additions (`apps/brain/src/brain/schema.sql` + idempotent migration in `BrainDB._init_schema`):**

```sql
CREATE TABLE IF NOT EXISTS trace_runs (
    trace_id TEXT PRIMARY KEY,
    turn_id TEXT,
    source TEXT NOT NULL,            -- 'voice' | 'ask' | 'board' | 'timer' | 'cli'
    surface TEXT NOT NULL,           -- key in HARNESS_SURFACES
    runner TEXT,                     -- 'direct' | 'ephemeral' | 't3' | 'claude' | 'opencode' | 'pi'
    repo TEXT,
    task_id TEXT,
    attempt_id INTEGER,
    outcome TEXT NOT NULL,           -- 'ok' | 'friction' | 'failed_verify' | 'timed_out' | 'errored' | 'cancelled'
    friction_kind TEXT,              -- NULL | 'replaced' | 'stopped' | 'not_you' | 'repeated' | 'undo_mute' | 'asr_rewrite' | 'router_override' | 'latency_sla'
    causal_sig TEXT,                 -- Self-Harness phi(r): '<cause>|<behavior>|<mechanism>'
    latency_ms INTEGER,
    ttfb_ms INTEGER,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    git_sha TEXT,
    prompt_sha TEXT,
    skills_used TEXT NOT NULL DEFAULT '[]',
    user_input TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    trace_path TEXT NOT NULL,
    superseded_by TEXT,              -- points to the correcting trace_id when friction occurred
    reflected INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_trace_runs_created ON trace_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trace_runs_surface ON trace_runs(surface, outcome, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trace_runs_sha ON trace_runs(git_sha, outcome);

CREATE VIRTUAL TABLE IF NOT EXISTS trace_fts USING fts5(
    trace_id UNINDEXED,
    user_input,
    output_summary,
    causal_sig,
    tokenize='porter unicode61'
);
```

**Online Implicit Friction State Machine (`ImplicitFrictionDetector` in `traces.py`):**
Maintains the last 20 finished/running `trace_runs` in RAM (`deque(maxlen=20)`) and applies deterministic
back-patching via `db.mark_trace_friction(old_trace_id, friction_kind, causal_sig, surface, superseded_by)`:

1. **`not_you` / `undo_mute` / `repeated`:** Triggered directly when `FeedbackLog._correct()` (`feedback.py:128`)
   emits `voice.feedback` → marks the earlier turn `outcome='friction'`, `surface='voice_intent_data'`,
   `causal_sig='intent_misclassified|<acted_intent>|speech_gate'`.
2. **`asr_rewrite`:** When `voice.cleanup` changes $\ge 35\%$ of words in `voice.stt` (or rewrites a recognized
   project slug from `list_projects()`) → marks `friction_kind='asr_rewrite'` (outcome stays `'ok'` if the turn
   succeeded, flagging training data for `whisper_asr_data` without counting as a task failure).
3. **`replaced`:** When `POST /v1/runs` arrives with `replace=True` → marks the interrupted run's `trace_id`
   `outcome='friction'`, `friction_kind='replaced'`, `superseded_by=new_trace_id`. If `project` or `runner`
   changed between the two, `surface='brain_router'`; otherwise `surface='voice_prompts'`.
4. **`router_override`:** When a new turn arrives within 60s of a completed/failed run with word overlap
   $\ge 0.5$ and adds an explicit project or runner name → back-patches the prior run with
   `outcome='friction'`, `friction_kind='router_override'`, `surface='brain_router'`,
   `causal_sig='wrong_target|<old_runner_or_repo>|router_disambiguation'`.
5. **`stopped`:** When `POST /v1/runs/:id/stop` cancels a run and no `replace=True` follows within 60s →
   `outcome='cancelled'`, `friction_kind='stopped'`, `causal_sig='user_abort|<runner>|wrong_or_slow_execution'`.
6. **`latency_sla`:** When `voice.latency` reports total turn latency `> 2500 ms` on a direct reply or
   `brain.route` phase takes `> 800 ms` → records `friction_kind='latency_sla'` with the slowest stage in
   `causal_sig`.
7. **`failed_verify` / `timed_out` / `tool_loop`:** On board/runner failures, builds $\phi(r)$ from
   `(outcome, repeated_error_tools or last_tool or phase, surface)` instead of collapsing everything to exit
   codes.

---

### Step 5: Layered Distillation (`traces/analysis/`), Statistical SHA Attribution & Nightly `ASSIST` Timer

**1. `distill_experience(db, traces_dir)` (`apps/brain/src/brain/traces.py`):**
Called automatically at the start of `Reconciler.run_skill_improver()` (`reconciler.py:913`):
- Queries `trace_runs` over the last 7 days + all `reflected = 0` rows.
- **Statistical regression check across `git_sha` (solves AHE RQ3b regression blindness):** Compares the
  current `git_sha`'s failure+friction rate, median latency, and `Succ/Mtok` (`ok_runs * 1e6 / total_tokens`)
  against the previous `git_sha` (when both have $\ge 10$ runs). Highlights any surface whose friction/failure
  rate jumped by $\ge 10\text{ pp}$ or token cost jumped $\ge 25\%$.
- **Partial-pass pairing (*AHE* `evolve_prompt.md`):** Finds `(repo, surface)` pairs that have both a failed/friction
  trace and a subsequent `ok` trace (`superseded_by` or retry), linking both JSON paths side by side so the
  Improver can diff why the retry succeeded.
- Writes:
  - `traces/analysis/overview.md` ($\le 120$ lines: health by `git_sha`, `Succ/Mtok`, top $\phi(r)$ clusters
    ranked by support, partial-pass pairs, and pointers to `surfaces/<id>.md`).
  - `traces/analysis/surfaces/<surface>.md` (up to 15 representative records per surface with `trace_path`,
    `causal_sig`, input/output previews, and `gate_cmd`).
- Updates `run_skill_improver()` prompt to read `traces/analysis/overview.md` first, drill into
  `surfaces/<id>.md`, and `read`/`grep` `traces/YYYY-MM-DD/trace-<id>.json` only for the specific cases it acts on.

**2. Nightly reflection in `ASSIST` mode (`reconciler.py`):**
- Today `reconciler.py:301` gates `run_skill_improver()` behind `if state.mode == "AUTONOMOUS":`, while
  `default_mode = "ASSIST"`.
- In `Reconciler.__init__` (or `tick()`), track `_last_reflection_date`: after `03:00` local time (configurable
  via `nightly_reflect_cron`), if `db.count_unreflected_traces() >= 5` and the board has no active run, run
  `run_skill_improver()` regardless of `ASSIST` vs `AUTONOMOUS` mode. (Harness code fixes still land as `draft`
  tasks unless `AUTONOMOUS_HARNESS_UPGRADES=true`, so daytime `ASSIST` safety is completely preserved.)

---

### Step 6: `jarvis traces` CLI, Cockpit Cold-Start Hydration & Optional OTLP Export

1. **Operator HTTP routes (`server.py` + `schemas/openapi.json`) & CLI (`src/jarvis/cli.py`):**
   - `GET /v1/traces` (`?outcome=`, `?surface=`, `?q=` FTS5 query, `?limit=30`) and
     `GET /v1/traces/{trace_id}` (returns the stitched `trace-<id>.json`).
   - `GET /v1/traces/diff?base=<sha1>&head=<sha2>` (per-surface pass/friction, p50 latency, cost, and `Succ/Mtok`
     comparison).
   - `jarvis traces` subcommand:
     - `jarvis traces` — last 20 turns/runs (time, source, surface, runner, latency, cost, outcome/friction).
     - `jarvis traces --friction` / `--failed` / `--surface brain_router` / `--search "login"`.
     - `jarvis traces <trace_id>` — pretty-prints every stage of that turn/run.
     - `jarvis traces diff <sha1>..<sha2>` — regression table between two commits.
2. **Cockpit cold-start X-Ray (`apps/tui/src/{db.rs,app.rs,ui/xray.rs}`):**
   - In `apps/tui/src/db.rs`, query the newest 50 `trace_runs` rows (`SELECT trace_id, turn_id, trace_path, ...`)
     on startup (and when new rows appear without live SSE, e.g. `/v1/ask` from iPhone) and hydrate `Trace`
     stage lists from `trace-<id>.json` off the UI thread, deduplicating against live SSE `turn_id`/`trace_id`.
3. **Optional zero-dependency OTLP/HTTP JSON exporter (`traces.py`):**
   - When `settings.otel_exporter_otlp_endpoint` is non-empty (e.g. `http://127.0.0.1:6006/v1/traces` for
     `uvx arize-phoenix serve` or `http://127.0.0.1:10428/insert/opentelemetry/v1/traces` for `victoria-traces`),
     `TraceSink` batches completed OTLP JSON `resourceSpans` payloads via `httpx.AsyncClient` (`timeout=2.0s`,
     best-effort, warn-once on connection failure). Zero extra packages, zero RAM when unset.

---

## 3. Execution Order & Verification Gates

Implement and merge in **four small, independently testable commits**:

1. **Commit 1 — Correlation, Redaction & `TraceSink` (`Steps 1–3`):**
   - Build `traces.py` (`HARNESS_SURFACES`, redaction, daily JSONL + `trace-<id>.json`, retention), wire
     `turn_id` + `trace_id` + `replace` through `jarvis_voice/telemetry.py`, `agent_tools.py`, `brain_client.py`,
     `server.py`, and `orchestrator.py`.
   - **Verify:** `uv run pytest apps/brain/tests/test_traces.py apps/voice/tests` + `uv run mypy` + `uv run ruff check`.
     Explicit assertions that `pair.requested` digits, `SECRET_ENV` values, and `[Room]` background speech never
     appear in `traces/`.
2. **Commit 2 — `trace_runs` + FTS5 + 8-Signal Friction Detector (`Step 4`):**
   - Add `schema.sql` tables, `BrainDB` methods, `ImplicitFrictionDetector`, and `feedback.py` `voice.feedback`
     hook.
   - **Verify:** Unit tests covering all 8 friction state transitions, FTS5 Porter query search, and SHA
     comparison queries.
3. **Commit 3 — Layered Distillation, `PROTECTED_SELF_PATHS` & Nightly Reflection (`Step 5`):**
   - Wire `distill_experience()` (`overview.md` + `surfaces/*.md` + `git_sha` regression alerts + partial-pass
     pairs) into `run_skill_improver()`, add `apps/brain/src/brain/traces.py` to `PROTECTED_SELF_PATHS`, and
     enable the `03:00` idle reflection check in `ASSIST` mode.
   - **Verify:** `uv run pytest apps/brain/tests/test_skills.py apps/brain/tests/test_reconciler.py` verifying
     Seatbelt read-denial on `traces/` for `Builder`/`Planner` and read-allow for `Improver`.
4. **Commit 4 — `jarvis traces` CLI, Cockpit Cold-Start History & Optional OTLP (`Step 6`):**
   - Add `/v1/traces*` routes, `schemas/openapi.json` entries, `jarvis traces` CLI, Rust `apps/tui` cold-start
     loader, and optional `OTEL_EXPORTER_OTLP_ENDPOINT` poster.
   - **Verify:** `just lint && just test` (`pytest` + `ruff` + `mypy` + `cargo test --workspace` +
     `cargo clippy --workspace --all-targets -- -D warnings`).

---

## 4. Footprint & Risk Summary

- **Steady-state footprint:** `< 5 MB` extra RSS in `jarvis`, `0` extra processes, `~15–30 MB` disk for 30 days
  of voice + coding traces (capped hard at `500 MB`), `0 ms` added latency on the audio/TTS path.
- **Known limits (`shortcut:` comments to place in code):**
  - `shortcut: prompt_sha hashes shipped prompts + SYSTEM.md at turn time; T3's own internal system prompt is external and unhashed.`
  - `shortcut: OTLP exporter uses OTLP/HTTP JSON via httpx rather than protobuf/gRPC so grpcio/protobuf stay out of .venv.`
