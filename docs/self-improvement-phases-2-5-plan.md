# Phases 2–5 Implementation Plan: Evaluators, Closed Loop, Pareto Skills & Weights (plan, 2026-11-24)

Status: **not started** (baseline: `c31bf80`, Phase 1 landed).
Companions: `docs/self-improvement-plan.md` (roadmap & literature) and `docs/observability-plan.md` (Phase 1 build spec).

---

## 0. Design Principles & Non-Negotiables

1. **Zero new daemons, zero Docker, zero new runtime dependencies in `.venv`.** Everything runs inside the
   existing `jarvis` process using SQLite (`state.db`), `httpx`, Ollama's already-pinned `bge-m3` endpoint
   (`http://127.0.0.1:11434/api/embed`), and PEP 723 standalone scripts (`uv run scripts/...`) for PyTorch/MLX
   weight training.
2. **Evaluators and held-out splits stay outside every worker's reach.** Benchmark scripts live in
   `PROTECTED_SELF_PATHS` and are executed from `repo_dir` (never from the candidate `worktree_dir`).
   `JARVIS_HOME/evals/held_out/` is read-denied in `sandbox.py` to **every** worker role (`Planner`, `Builder`,
   `Scout`, and `Improver`), so neither a Builder nor an Improver task spec can leak held-out prompts.
3. **One process, one Qdrant lock.** `src/jarvis/supervisor.py` runs `jarvis-voice` and `brain` in the same
   Python process, and embedded Qdrant (`JARVIS_HOME/memory/qdrant`) holds an exclusive file lock. Phase 3's
   Mem0 curation reuses the in-process `Mem0` handle registered by `supervisor.py` (falling back to a short-lived
   handle only when `brain` runs standalone), never spawning a competing client.
4. **Hard promotion order.** `AUTONOMOUS_HARNESS_UPGRADES` stays `false` until Phase 2's router and voice-prompt
   gates are green in CI. Cockpit SQLite reads (`apps/tui/src/db.rs`) remain strictly `SQLITE_OPEN_READ_ONLY`,
   and any new `state.db` columns use nullable defaults via idempotent `ALTER TABLE` migrations.

---

## Phase 2 — Evaluator Coverage & Sealed Held-In / Held-Out Splits

**Goal:** Every edit to `BrainOrchestrator.route`, `TranscriptCleaner`, `_retell`, `WAITING_PROMPT`, or
`SYSTEM.md` is gated by a deterministic accuracy + latency score on an editable `held_in` split and zero
regression on a worker-sealed `held_out` split. Also closes the two remaining Phase 1 loose ends.

### 2.1 File Inventory

| Action | Path | Est. LOC | Purpose |
| :--- | :--- | :---: | :--- |
| **New** | `apps/brain/src/brain/evals.py` | ~140 | Deterministic 80/20 `held_in`/`held_out` splitter and nightly harvester from `feedback/intent.jsonl` + `trace_runs` friction pairs. |
| **New** | `scripts/bench_brain_route.py` | ~160 | Benchmark `BrainOrchestrator.route` across all 5 `ROUTER_TOOLS` + `--split held_in\|held_out`; enforces `>= 95%` accuracy and `p50 <= 800 ms`. |
| **New** | `scripts/bench_voice_prompts.py` | ~150 | Invariant benchmark for `TranscriptCleaner.clean`, `_retell`, and `waiting_lines` (plus active `SYSTEM.md` overrides). |
| **New** | `apps/brain/tests/test_evals.py` | ~130 | Split determinism, secret/room scrubbing during harvest, Seatbelt `held_out` read-denial, and gate failure on planted router regression. |
| **Modify** | `apps/brain/src/brain/skills.py` | ~-15 / +10 | Phase 1 cleanup: delete duplicate `_surface_for_trace` vocabulary and delegate `mine_failure_patterns` to `traces.classify_surface` / `HARNESS_SURFACES`. |
| **Modify** | `apps/brain/tests/test_traces.py` | ~30 | Phase 1 cleanup: add missing unit test that `_nightly_reflection_due()` fires `run_skill_improver` while board mode is `ASSIST`. |
| **Modify** | `apps/voice/src/jarvis_voice/system_prompts.py` | ~30 | Add `validate_overrides(overrides)` startup guard (length bounds, preserved `<transcript>` contract, logged SHA-256). |
| **Modify** | `apps/brain/src/brain/sandbox.py` | ~10 | Deny `file-read-data` on `JARVIS_HOME / "evals" / "held_out"` unconditionally across all worker roles. |
| **Modify** | `apps/brain/src/brain/reconciler.py` & `traces.py` | ~75 | Protect `evals.py` and new `bench_*.py` scripts; add `BRAIN_ROUTE_PATHS` & `VOICE_PROMPT_PATHS`; add `apps/brain/src` to benchmark `extra_pythonpath`; enforce held-in fix + zero held-out regression; update `HARNESS_SURFACES` `gate_cmd`s. |

### 2.2 Subsystem Specification

1. **Phase 1 carry-over cleanup (`skills.py`, `test_traces.py`):**
   - Replace `skills._surface_for_trace` (`skills.py:417`) with `traces.classify_surface`, emitting canonical
     `HARNESS_SURFACES` keys (`builder_harness`, `skills`, `voice_intent_data`) instead of legacy prose strings.
   - Add `test_nightly_reflection_fires_in_assist_mode` in `apps/brain/tests/test_traces.py` seeding 5
     unreflected `trace_runs` rows with `mode == 'ASSIST'` after `nightly_reflect_hour`.
2. **Auto-harvester & deterministic split (`apps/brain/src/brain/evals.py`):**
   - Storage (`0700` dirs, `0600` JSONL files):
     ```text
     JARVIS_HOME/evals/
     ├── held_in/{intent.jsonl, brain_route.jsonl, voice_prompts.jsonl}
     └── held_out/{intent.jsonl, brain_route.jsonl, voice_prompts.jsonl}
     ```
   - Split function: normalize input (`" ".join(re.findall(r"\w+", text.lower()))`); route to `held_out` iff
     `int.from_bytes(hashlib.sha256(norm.encode()).digest()[:4], "big") % 5 == 0`, else `held_in`. Deduplicate by
     `norm` hash so rephrased punctuation never leaks across splits.
   - `harvest_eval_cases(db, jarvis_home)` runs at the start of `Reconciler.run_skill_improver()` right after
     `distill_experience()`:
     - **`intent.jsonl`:** Confirmed corrections in `JARVIS_HOME/feedback/intent.jsonl` where `correction_of`
       resolves an earlier model decision (`source in {"laya", "jev"}`).
     - **`brain_route.jsonl`:** `trace_runs` rows with `friction_kind IN ('router_override', 'replaced')` whose
       `superseded_by` row has `outcome = 'ok'`, recording `(original.user_input, resolved_choice, resolved_project, resolved_runner)`.
     - **`voice_prompts.jsonl`:** `trace_runs` rows with `friction_kind = 'asr_rewrite'` and `outcome = 'ok'`,
       extracting `(voice.stt, voice.cleanup)` from `trace-<id>.json`.
     - Every harvested string passes `traces._scrub` before disk; `[Room]` unaddressed lines are skipped.
3. **Brain router benchmark (`scripts/bench_brain_route.py`):**
   - Instantiates `BrainOrchestrator` with stub runners and a fixed project/session fixture (`HuggerHustle/platform-v2`,
     `opscom/status`, `jarvis`), calling the real OpenRouter model (`cfg.router_model`) plus `_heuristic` fallback.
   - Ships ~48 seed cases across `delegate_project` (explicit project, implicit latest, `+new`, named `claude`/`opencode`),
     `ask_ephemeral_pi`, `list_sessions`, `queue_board_task`, and `reply_direct`, plus `--split held_in|held_out|all`
     and `--dump-results <path.json>` (per-case pass/fail map for regression diffing).
   - Exits `1` if `accuracy < 0.95` or `p50_ms > 800`. Prints `METRIC: <accuracy>` and
     `METRICS: {"accuracy": ..., "p50_ms": ..., "cost_usd": ...}` for Phase 4 tournament parsing.
4. **Voice prompt invariant benchmark (`scripts/bench_voice_prompts.py`) & `SYSTEM.md` guard:**
   - Pure deterministic invariants over live model outputs (no LLM-as-judge):
     - **`cleanup`:** Spoken self-corrections ("No wait, 24 not 14") resolve to final intent; repo slugs and
       identifiers (`pyproject.toml`, `HuggerHustle/platform-v2`, `git rebase --onto`) survive intact;
       `keep_cleaned()` holds; clean `< 18`-word commands satisfy `should_clean() is False` (`0 ms`).
     - **`retell`:** Plain completion reports stay `<= 20` words, emit zero tool calls, strip markdown/code
       fences and internal agent names (`pi`, `T3 Code`), and multi-option decision turns preserve `<= 4` option
       headings plus the trailing question.
     - **`waiting`:** Produces 6 lines `<= 10` words each with zero progress fabrications (`/\b(\d+%|almost done|tests pass|compiling)\b/i`).
   - `system_prompts.py`: add `validate_overrides(overrides)` rejecting empty/oversized sections (`> 4000` chars)
     or a `## Cleanup` override that drops `<transcript>` framing.
5. **Sandbox sealing & self-upgrade gate (`sandbox.py`, `reconciler.py`):**
   - `sandbox.py` `_deny_reads`: append `(deny file-read-data (subpath "<JARVIS_HOME>/evals/held_out"))`
     regardless of `allow_read_traces`.
   - `reconciler.py`:
     - Add `apps/brain/src/brain/evals.py`, `scripts/bench_brain_route.py`, `scripts/bench_voice_prompts.py` to
       `PROTECTED_SELF_PATHS`.
     - Add `BRAIN_ROUTE_PATHS = {"apps/brain/src/brain/orchestrator.py"}` and
       `VOICE_PROMPT_PATHS = {"apps/voice/src/jarvis_voice/cleanup.py", "apps/voice/src/jarvis_voice/prompts.py", "apps/voice/src/jarvis_voice/state.py", "apps/voice/src/jarvis_voice/system_prompts.py", "apps/voice/src/jarvis_voice/waiting_lines.py", "SYSTEM.md"}`.
     - In `_run_voice_benchmarks` (`reconciler.py:1994`), add `worktree_dir / "apps" / "brain" / "src"` to
       `sources` so candidate `orchestrator.py` edits are actually imported under test.
     - When `evals/held_out/<suite>.jsonl` exists, run both baseline (`repo_dir` `PYTHONPATH`) and candidate
       (`worktree_dir` `PYTHONPATH`) with `--dump-results`: require `0` newly failing `held_out` cases, and when
       `task.dedupe_key` starts with `"harness:"` and `held_in` has prior failures, require `>= 1` newly passing
       `held_in` case. Output from `held_out` stays sealed from worker `verify_output`.

### 2.3 Commits & Verification Gates

1. **Commit 1 — Phase 1 Loose Ends + `evals.py` + Seatbelt `held_out` Seal:**
   - Unify `skills.py` onto `classify_surface`, add `ASSIST` nightly reflection test, implement `evals.py` +
     `sandbox.py` rule.
   - **Gate:** `uv run pytest apps/brain/tests/test_evals.py apps/brain/tests/test_traces.py apps/brain/tests/test_skills.py`.
2. **Commit 2 — `bench_brain_route.py`, `bench_voice_prompts.py` & Self-Upgrade Gate Wiring:**
   - Land both benchmark scripts, `system_prompts.py` guard, and `reconciler.py` baseline-vs-candidate held-in /
     held-out comparison.
   - **Gate:** `just lint && just test`, plus automated test in `test_self_upgrade.py` proving a deliberately
     broken `orchestrator.py` candidate is rejected by `_run_voice_benchmarks` while a valid fix passes.

---

## Phase 3 — Closed Decision Loop & One Memory

**Goal:** Give the Improver durable memory of its own proposal history, verify every pushed harness prediction
over $K=10$ real post-upgrade runs on its target surface, and unify Brain's `MEMORY.md` with daytime routing/T3
threads and Voice Mem0.

### 3.1 File Inventory

| Action | Path | Est. LOC | Purpose |
| :--- | :--- | :---: | :--- |
| **New** | `apps/brain/src/brain/voice_memory.py` | ~110 | In-process Qdrant-safe `curate_voice_memory` helper enforcing `sang` vs `speaker:*` namespace isolation. |
| **New** | `apps/brain/tests/test_closed_loop.py` | ~180 | Per-surface dedupe, duplicate-rejection RPC block, $K=10$ falsifiability rollback alert, and Mem0 namespace wall tests. |
| **Modify** | `apps/brain/src/brain/schema.sql` & `db.py` | ~110 | Add `harness_proposals` table + CRUD, surface baseline rate query, and $K=10$ post-upgrade surface evaluator. |
| **Modify** | `apps/brain/src/brain/defaults/pi/board.ts` | ~35 | Add `target_surface` enum to `propose_harness_task` and register Improver-only `curate_voice_memory` tool (owner-reviewed: `board.ts` is in `PROTECTED_SELF_PATHS`). |
| **Modify** | `apps/brain/src/brain/reconciler.py` & `server.py` | ~95 | Enforce `harness:<surface>` dedupe + duplicate block, inject last 10 proposals into Improver prompt, sync `rejected`/`failed_verify`/`rolled_back` status, and run $K=10$ falsifiability check. |
| **Modify** | `apps/brain/src/brain/orchestrator.py` & `runners/t3.py` | ~35 | Prepend prefix-stable `MEMORY.md` to `ROUTER_PROMPT` and standing repo context to `T3Runner._launch`. |
| **Modify** | `apps/voice/src/jarvis_voice/main.py` & `src/jarvis/supervisor.py` | ~20 | Pass active `Mem0` reference into `BrainServer`/`Reconciler` so curation shares the open Qdrant lock. |

### 3.2 Subsystem Specification

1. **`harness_proposals` ledger & per-surface dedupe (`schema.sql`, `db.py`, `reconciler.py`):**
   ```sql
   CREATE TABLE IF NOT EXISTS harness_proposals (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       task_id TEXT REFERENCES tasks(id),
       surface TEXT NOT NULL,
       spec_hash TEXT NOT NULL,
       title TEXT NOT NULL,
       spec TEXT NOT NULL,
       predicted_fix TEXT NOT NULL DEFAULT '',
       at_risk_regressions TEXT NOT NULL DEFAULT '',
       baseline_fail_rate REAL NOT NULL DEFAULT 0.0,
       baseline_runs INTEGER NOT NULL DEFAULT 0,
       applied_sha TEXT,
       rollback_sha TEXT,
       outcome TEXT NOT NULL DEFAULT 'proposed' CHECK (
           outcome IN ('proposed', 'pushed', 'failed_verify', 'rejected', 'rolled_back', 'falsified', 'verified')
       ),
       created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
       updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
   );
   ```
   - `propose_harness_task` requires `target_surface` in `HARNESS_SURFACES` and sets `dedupe_key = f"harness:{surface}"`
     (replacing global `HARNESS_DEDUPE_KEY`).
   - `baseline_fail_rate` and `baseline_runs` are computed in Python from the last 50 `trace_runs` rows on
     `surface` (`outcome != 'ok' OR friction_kind IS NOT NULL`), never self-reported by the model, and written
     into both `harness_proposals` and the `outbox` `details`.
2. **Duplicate proposal blocker & history injection (`reconciler.py`, `server.py`):**
   - When an owner rejects an outbox item or cancels/blocks a `harness:*` task (`server.py`), or a Builder
     finishes `failed_verify` / rolls back (`reconciler.py`), update `harness_proposals.outcome`.
   - In `handle_rpc("propose_harness_task")`, check recent (`30d`) `harness_proposals` on `surface` with
     `outcome IN ('rejected', 'failed_verify', 'rolled_back', 'falsified')`: if `spec_hash` matches or
     `spec_similarity(spec, prev.spec) >= 0.8`, raise `ValueError` quoting the prior outcome so the Improver
     cannot re-queue the same idea unchanged.
   - Inject a 10-line summary of the newest 10 `harness_proposals` into `run_skill_improver()`'s prompt.
3. **Post-upgrade falsifiability check (`db.py`, `reconciler.py`):**
   - Keep the existing 2-crash circuit breaker (`record_post_upgrade_outcome`) for immediate broken commits.
   - Additionally, when `_apply_self_upgrade` applies a `harness:<surface>` task, record `applied_sha` and
     `rollback_sha = old_head` on its `harness_proposals` row.
   - In `Reconciler.tick()`, for each `outcome = 'pushed'` proposal, once `trace_runs` accumulates $K = 10$ rows
     with `git_sha = applied_sha` and `surface = row.surface`:
     - Compute `post_fail_rate`. If `post_fail_rate > row.baseline_fail_rate` (and `>= 2` failures), mark
       `outcome = 'falsified'` and queue an `approval_request` outbox item quoting `predicted_fix`,
       `baseline_fail_rate -> post_fail_rate`, and `rollback_sha` (resolving `approved` resets to `rollback_sha`).
     - Otherwise mark `outcome = 'verified'`.
4. **One memory across Brain, T3, and Voice (`orchestrator.py`, `runners/t3.py`, `voice_memory.py`):**
   - **`ROUTER_PROMPT`:** Prepend `memory/MEMORY.md` (`<= 150` lines) at the **very top** of the system message
     before dynamic `{projects}`/`{sessions}`/`{spent}` fields so prompt caching keeps its prefix warm (`~0 ms`
     latency delta).
   - **`T3Runner._launch` (`t3.py:649`):** On new thread creation only, prepend non-empty `MEMORY.md` +
     `memory/repos/<repo_slug>.md` as a brief `[Standing context]` header ahead of `SYNC_MASTER + request.prompt`.
   - **`curate_voice_memory` RPC (`voice_memory.py`):**
     - `supervisor.py` attaches a lightweight getter for `LastExchangeMemory`'s underlying `Mem0` client onto
       `Reconciler` (standalone `brain` opens Mem0 lazily only if `qdrant/.lock` is free, else skips cleanly).
     - Operations exposed to `Improver` (max 1 call per reflection run): `merge_duplicates`, `delete_contradicted`,
       and `promote_to_brain_memory` (appends verified durable engineering facts to `MEMORY.md` via
       `write_memory_file`).
     - **Hard code boundary:** `user_id` is validated in Python. `promote_to_brain_memory` accepts **only**
       `user_id == "sang"`; `speaker:<name>` namespaces may only deduplicate/delete within their own exact
       `user_id` and can never cross namespaces or touch `MEMORY.md`.

### 3.3 Commits & Verification Gates

1. **Commit 1 — `harness_proposals`, Per-Surface Dedupe, History Block & $K=10$ Falsifiability:**
   - **Gate:** Integration test in `test_closed_loop.py` proving (a) two surfaces can hold active proposals
     concurrently, (b) a rejected proposal hard-blocks a reworded duplicate (`similarity >= 0.8`), and (c) 10
     regressing post-upgrade `trace_runs` trigger a `falsified` rollback outbox item.
2. **Commit 2 — `MEMORY.md` Injection & Namespace-Isolated `curate_voice_memory`:**
   - **Gate:** Unit + integration tests proving `ROUTER_PROMPT` and `T3Runner._launch` include `MEMORY.md`,
     in-process Mem0 handle is reused without locking errors, and attempting to promote a `speaker:david` memory
     into `MEMORY.md` raises `ValueError`.

---

## Phase 4 — Executable Skills, Marginal Lift & Pareto Tournament

**Goal:** Let skills carry tiny sandboxed context/check scripts, automatically retire placebo skills with no
marginal lift after 10 loads, upgrade hypothesis novelty to `bge-m3` embeddings with `auto_research_after_strikes = 2`,
and select tournament winners on `(accuracy, p50_ms, cost_usd)` within bounded evolve blocks.

### 4.1 File Inventory

| Action | Path | Est. LOC | Purpose |
| :--- | :--- | :---: | :--- |
| **New** | `apps/brain/src/brain/pareto.py` | ~95 | Multi-objective `METRICS:` parser, Pareto dominance filter, `+150 ms` voice latency guardrail, and bounded `EVOLVE-BLOCK` diff verifier. |
| **New** | `apps/brain/tests/test_pareto_and_lift.py` | ~170 | Placebo skill retirement at 10 loads, read-only/no-net Seatbelt enforcement on `context.py`/`check.sh`, `bge-m3` fallback, and `+0.5% acc / +180 ms` tournament rejection. |
| **Modify** | `apps/brain/src/brain/skills.py` & `db.py` | ~110 | Optional `context.py` / `check.sh` validation & disk sync; `compute_skill_lift()` against repo baseline and auto-retirement in `record_skill_outcome`. |
| **Modify** | `apps/brain/src/brain/sandbox.py` | ~25 | Add `wrap_readonly_skill_helper()` (`(deny file-write* (subpath "/"))` + `(deny network*)`). |
| **Modify** | `apps/brain/src/brain/defaults/pi/board.ts` | ~35 | Optional `context_py` / `check_sh` fields on `propose_skill_change`; auto-append `context.py` stdout when a worker reads `skills/<slug>/SKILL.md` (or via `run_skill_check`). |
| **Modify** | `apps/brain/src/brain/config.py` & `reconciler.py` | ~85 | Default `auto_research_after_strikes: int = 2`; async `semantic_spec_similarity()` via Ollama `bge-m3` (`>= 0.88`, word-Jaccard fallback); wire `pareto.py` into `'any'` sibling tournament. |
| **Modify** | `apps/brain/src/brain/orchestrator.py` & `apps/voice/src/jarvis_voice/prompts.py` | ~12 | Wrap `ROUTER_PROMPT`, `CLEANUP_PROMPT`, `AGENT_REPORT_PROMPT`, and `WAITING_PROMPT` in `# === EVOLVE-BLOCK: <name> ===` markers. |

### 4.2 Subsystem Specification

1. **MCE-lite executable skills (`skills.py`, `sandbox.py`, `board.ts`):**
   - `skills/<slug>/` may include `context.py` (`<= 80` lines, AST-checked: stdlib imports only, banning
     `socket`, `http`, `urllib`, `ctypes`, `multiprocessing`, plus `FORBIDDEN_PATTERNS`) or `check.sh` (`<= 40`
     lines, checked against `FORBIDDEN_PATTERNS` + `BLOCKED_BASH_PATTERNS`). Serialized in a JSON column
     `sidecars_json TEXT NOT NULL DEFAULT '{}'` on `skills` (snapshotted alongside `previous_content` for atomic
     rollback).
   - Execution runs in `wrap_readonly_skill_helper`: Seatbelt denies **all** `file-write*` (except `/dev/null`),
     denies all `network*`, denies `.env` / `~/.ssh` / `state.db*` / `traces/` / `evals/held_out/`, strips
     `SECRET_ENV`, sets `cwd=worktree_dir`, enforces `timeout=10s`, and caps stdout at `4096` bytes.
   - When `record_skill_load` fires for a skill with `context.py`, `reconciler.py` runs it once per attempt and
     returns the verified context snippet to `board.ts`.
2. **Marginal lift & 10-load placebo retirement (`skills.py`, `db.py`):**
   - No extra counters needed: join `attempts` + `tasks` over the last 60 days on repos where `slug` was loaded,
     splitting attempts into `with_skill` (`slug in skills_used`) vs `repo_baseline` (`slug not in skills_used`).
   - Compute `pass_lift = pass_rate(with_skill) - pass_rate(repo_baseline)` and
     `cost_ratio = median_cost(with_skill) / max(0.01, median_cost(repo_baseline))`.
   - In `record_skill_outcome`, once `loads_since_update >= 10`: if `len(repo_baseline) >= 3` and
     `pass_lift <= 0.0` and `cost_ratio >= 0.95` (neither higher pass rate nor `>= 5%` cheaper; fallback when
     `< 3` baseline runs: `successes_since_update / loads_since_update < 0.5`), retire `slug` and queue a `report`
     outbox item explaining the zero-lift retirement.
3. **`auto_research_after_strikes = 2` & `bge-m3` novelty (`config.py`, `reconciler.py`):**
   - Flip `Settings.auto_research_after_strikes` default from `0` to `2` (`reconciler.py:747` already guards
     `parent_id is None` so child hypotheses never recurse; keep `auto_research_after_strikes=0` in
     `brain_fakes.py` unit-test defaults except in `test_auto_research.py`).
   - Before flipping the cosine threshold on, run a one-time check over historical task specs in `test_pareto_and_lift.py`
     confirming `cosine >= 0.88` separates distinct mechanisms while catching paraphrases.
   - `semantic_spec_similarity(left, right)` posts `{"model": "bge-m3", "input": [left, right], "keep_alive": -1}`
     to `http://127.0.0.1:11434/api/embed` (`timeout=1.5s`). If Ollama is down or unpulled, falls back to
     `spec_similarity(left, right) >= 0.80`.
4. **Bounded evolve blocks & Pareto tournament (`pareto.py`, `reconciler.py`):**
   - Wrap prompt constants in `# === EVOLVE-BLOCK: <id> ===` ... `# === END-EVOLVE-BLOCK ===`. When a `problem` /
     hypothesis task targets a prompt surface, verify `git diff -U0 base_sha` modifies lines **only** inside
     those markers (any edit outside requires protected-file owner approval).
   - `pareto.select_winner(candidates, baseline)`:
     1. Reject any candidate failing `verify_cmd`, `accuracy < floor`, or `held_out_regressions > 0`.
     2. **Voice/router latency guardrail:** reject any candidate with `delta_p50_ms > 150` unless
        `delta_accuracy >= 0.02` (blocks trading `+150 ms` of live speech latency for `+0.5%` accuracy).
     3. Prune Pareto-dominated candidates on `(+accuracy, -p50_ms, -cost_usd)`; rank survivors by
        `accuracy - 0.0001 * max(0.0, delta_p50_ms) - 0.05 * max(0.0, delta_cost_usd)` and record winner on
        `parent.best_metric` / `best_branch` / `best_sha`.

### 4.3 Commits & Verification Gates

1. **Commit 1 — MCE-Lite Read-Only Skill Sidecars & 10-Load Marginal Lift Retirement:**
   - **Gate:** Tests verifying AST/Seatbelt write+network denial on `context.py`/`check.sh` and automatic
     retirement of a 10-load zero-lift skill.
2. **Commit 2 — `bge-m3` Novelty, `auto_research_after_strikes=2`, Bounded Evolve Blocks & Pareto Selector:**
   - **Gate:** `just lint && just test`, including explicit assertion that a candidate buying `+0.5%` accuracy at
     `+160 ms` p50 latency loses the tournament to baseline.

---

## Phase 5 — Weight Updates & Meta-Utility Probation

**Goal:** Close the weight-update loop (`laya-intent`, `whisper-owner-lora`, `router-distill`) behind strict
data thresholds, human audio review, validation early-stopping, and atomic checkpoint symlinks — and measure the
Improver itself with a 7-day rolling meta-utility probation visible in `jarvis-top`.

### 5.1 File Inventory

| Action | Path | Est. LOC | Purpose |
| :--- | :--- | :---: | :--- |
| **New** | `apps/brain/src/brain/weights.py` | ~160 | Trigger threshold checks, dataset export/staging, checkpoint symlink manager (`current`/`previous`), and promotion gates. |
| **New** | `apps/brain/tests/test_weights_and_meta.py` | ~160 | Threshold rejection (`< 25` intent corrections), symlink promotion/rollback, 7-day probation rollback trigger, and SQLite meta-utility queries. |
| **Modify** | `scripts/voice/train.py` & `evaluate.py` | ~55 | Deterministic 10% `val` split carved from `train` (`test` untouched), epoch early-stopping on `val` WER/loss, and JSON summary output for automated gating. |
| **Modify** | `apps/brain/src/brain/defaults/pi/board.ts` & `reconciler.py` | ~65 | Register Improver-only `propose_weight_update`; snapshot 7-day meta-utility baseline when a self-upgrade edits `skills.py` or `run_skill_improver`; daily probation check. |
| **Modify** | `apps/brain/src/brain/schema.sql` & `db.py` | ~75 | Add 3 nullable probation columns on `system_state`; add `compute_meta_utility(window_days=7)` SQL query. |
| **Modify** | `src/jarvis/cli.py` | ~75 | Add `jarvis voice-review` (one-key owner review of staged `[Cleanup]` clips) and `jarvis models [status\|rollback <target>]`. |
| **Modify** | `apps/tui/src/{model.rs,db.rs,ui/mod.rs}` | ~80 | Read `system_state` probation columns + rolling 7-day meta-utility metrics read-only and render the probation/health strip in `jarvis-top`. |

### 5.2 Subsystem Specification

1. **Checkpoint layout & 1-command rollback (`apps/brain/src/brain/weights.py`, `src/jarvis/cli.py`):**
   - Every local model/checkpoint target lives under `JARVIS_HOME/models/<target>/` (`0700`):
     - Versioned immutable directories/manifests `v<N>-<YYYYMMDD>-<sha8>/`
     - Atomic symlinks `current` and `previous` (for remote-served `laya-intent` / `router-distill`,
       `current.json` and `previous.json` holding `{"model_id": ..., "promoted_at": ..., "eval_scores": ...}`).
   - `jarvis models rollback <target>` atomically swaps `current -> previous` and calls `POST /v1/restart`.
2. **`propose_weight_update(target)` RPC & per-target pipelines:**
   - Never runs training inside the `--offline` Improver turn; validates evidence counts and queues an `ASSIST`
     `draft` task (`dedupe_key = f"weights:{target}"`) so the owner approves compute spend.
   - **`whisper-owner-lora`:**
     1. When `SAVE_SEGMENTS` is on and an owner (`[User]`) turn logs `friction_kind = 'asr_rewrite'`, stage
        `{wav, raw_stt, cleaned}` into `JARVIS_HOME/voice_data/staged.jsonl`.
     2. `jarvis voice-review` plays/prints each staged item (`[y]es / [e]dit / [n]o`), appending approved clips
        into `voice_data/manifest.jsonl` with deterministic `split_of` (unreviewed `[Cleanup]` guesses never
        enter training). Requires `>= 20` newly reviewed clips since last checkpoint.
     3. `scripts/voice/train.py`: hold out deterministic 10% of `train` as `val` (`sha256(text) % 10 == 0`; the
        1-in-7 `test` split stays untouched), evaluate `val` loss/WER after each epoch, and keep the best `val`
        epoch (fixing Round 1's 5-epoch `loss ~ 0` memorization).
     4. Promote `current` symlink iff `evaluate.py --split test` strictly beats active owner WER **and**
        synthetic general-English WER degrades by `<= 0.2 pp`.
   - **`laya-intent`:**
     1. Requires `>= 25` new entries in `feedback/intent.jsonl` past `bookmarks.json["laya_intent_cursor"]` with
        either `correction_of` or high-confidence `laya != jev` disagreement.
     2. Exports scrubbed `feedback` + `evals/held_in/intent.jsonl` bundle and runs `LAYA_DISTILL_CMD` (defaulting
        to `~/Projects/laya/data/distill.py feedback` when present; queues a clean `blocker` outbox item if the
        `laya` repo/GPU box is not configured on this Mac).
     3. Promotes candidate `model_id` iff `scripts/bench_intent.py 3` (`>= 97%`) **and** sealed
        `evals/held_out/intent.jsonl` strictly beat `current.json`.
   - **`router-distill`:**
     1. Requires `>= 500` clean (`outcome = 'ok'`, `friction_kind IS NULL`) rows in `trace_runs` with
        `surface = 'brain_router'`; exports tool-call JSONL and gates candidate via `scripts/bench_brain_route.py 3`
        (`accuracy >= current`, `p50_ms <= 0.6 * current_p50_ms`, `0` `held_out` regressions).
3. **Meta-utility metrics & 7-day Improver probation (`schema.sql`, `db.py`, `reconciler.py`, `apps/tui`):**
   - Computed directly from existing `attempts` + `trace_runs` tables over rolling 7 days vs prior 7 days (zero
     extra ledger tables):
     - `first_attempt_pass_rate`: share of tasks whose chronologically first `builder` attempt has `outcome = 'pushed'`.
     - `friction_per_100_turns`: `100.0 * COUNT(friction_kind IS NOT NULL OR outcome != 'ok') / COUNT(*)` on
       `trace_runs WHERE source IN ('voice', 'ask')`.
     - `median_usd_per_solved_task` and `median_turns_per_solved_task` across tasks reaching `status = 'done'`.
   - Idempotent `system_state` columns: `improver_probation_until TEXT`, `improver_rollback_sha TEXT`,
     `improver_baseline_json TEXT`.
   - When `_apply_self_upgrade` touches `apps/brain/src/brain/skills.py` or `run_skill_improver` in
     `reconciler.py`, record `improver_rollback_sha = old_head`, `improver_baseline_json = compute_meta_utility()`,
     and `improver_probation_until = now + 7d` (kept independent of the 2-run `last_good_sha` check).
   - Daily check in `Reconciler.tick()`: once `>= 10` tasks and `>= 30` interactive turns have run under
     probation, if `first_attempt_pass_rate` drops `> 10 pp`, `friction_per_100_turns` rises `> 5.0`, or
     `median_usd_per_solved_task` rises `> 35%`, queue a rollback `approval_request` (or auto-rollback if
     `AUTONOMOUS_HARNESS_UPGRADES=true`) to `improver_rollback_sha`; clear columns cleanly when 7 days pass.
   - `apps/tui/src/{db.rs,model.rs,ui/mod.rs}`: query the 4 rolling-7d metrics + probation banner read-only in
     `load_snapshot` and display in the Skills/System header (`META 7d: pass1 78% · fric 5.4/100 · $0.38 · 5t` +
     amber `[IMPROVER PROBATION Nd @ sha]` badge when active).

### 5.3 Commits & Verification Gates

1. **Commit 1 — `weights.py`, Checkpoint Symlinks, `voice-review`, and Whisper Val Early-Stopping:**
   - **Gate:** Unit tests for threshold gating, `current`/`previous` symlink promotion + rollback, and `train.py`
     `val`/`test` disjointness check.
2. **Commit 2 — Rolling 7d Meta-Utility, 7-Day Improver Probation & Cockpit Strip:**
   - **Gate:** `just lint && just test` (`pytest`, `ruff`, `mypy`, `cargo test --workspace`, `cargo clippy --workspace --all-targets -- -D warnings`), verifying `apps/tui` renders both idle and active-probation snapshots against the updated `schema.sql`.

---

## Cross-Phase Summary, Order & Known Shortcuts

| Phase | Commits | Unlocks | Hard Prerequisite |
| :--- | :---: | :--- | :--- |
| **2. Evaluators & Splits** | 2 | Safe router/prompt self-upgrades (`AUTONOMOUS_HARNESS_UPGRADES`) | Phase 1 (`c31bf80`) |
| **3. Closed Loop & Memory** | 2 | Non-repeating Improver, $K=10$ falsifiability, unified Brain/Voice memory | Phase 2 `evals.py` & surface gates |
| **4. Executable Skills & Pareto** | 2 | Sandboxed `context.py`, placebo retirement, latency-aware tournament | Phase 2 `METRICS:` benchmark output |
| **5. Weights & Meta-Utility** | 2 | Parametric Laya/Whisper/Router updates + 7-day meta-probation in `jarvis-top` | Phases 2–3 held-out splits & traces |

- **Steady-state overhead across Phases 2–5:** `0` new processes, `0` new `.venv` packages, `< 2 MB` SQLite growth
  per quarter, `0 ms` added to TTS/STT audio frames, and prefix-cached `MEMORY.md` on `ROUTER_PROMPT`.
- **`shortcut:` comments to place in code:**
  - `shortcut: evals.py hashes normalized words (% 5 == 0) for the 80/20 split; upgrade to embedding clustering only if near-paraphrases leak across splits.`
  - `shortcut: semantic_spec_similarity falls back to word-Jaccard (>= 0.80) when local Ollama bge-m3 is offline so board work and CI never block on embeddings.`
  - `shortcut: laya-intent weight training shells out to LAYA_DISTILL_CMD and blocks cleanly when the laya repo/GPU host is absent on this Mac.`
