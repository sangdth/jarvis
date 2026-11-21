# Superbrain: Always-On Goal & Problem Reconciler on Mac mini (plan, 2026-11-21)

Status: Revised for v1 (2026-11-21).
Repository: `~/Projects/superbrain` (`sangdth/superbrain`, **private** repo, `AGPL-3.0-only` license matching `jarvis`).
Target: an always-on reconciler daemon (`superbrain`) running 24/7 on the **Mac mini**, reachable from **MacBook**
(`jarvis` via `HermesAdapter` over Tailscale) and **iPhone**, powered by ephemeral `pi 1.5.0` workers in isolated
worktrees.

---

## 1. Why & Multi-Device Topology

Jarvis (`src/jarvis/`) is a thin voice harness that routes coding to `t3-code` and everything else to `hermes`
(`hermes_adapter.py`). Putting the long-horizon "Superbrain" in its own private repo (`superbrain`) on the **Mac mini**
solves three problems at once:

1. **Always-on execution while MacBook sleeps.** The Mac mini never closes its lid. Overnight builders, timers, and
   morning briefs run on the Mac mini without draining the MacBook or touching open daytime T3 Code checkouts.
2. **Talk from MacBook or iPhone, one shared Brain.** Both devices talk to the same `superbrain` daemon on the Mac mini
   over Tailscale (`100.x.y.z` / `https://<mac-mini>.<tailnet>.ts.net:8643`).
3. **Zero changes to Jarvis in v1.** `superbrain` implements the exact 5-endpoint HTTP/SSE wire protocol that
   `HermesAdapter` (`src/jarvis/hermes_adapter.py`) already speaks.

```text
+-----------------------------------+       +-----------------------------------+
|      MACBOOK (Daytime Desk)       |       |         IPHONE (On the Go)        |
| - Runs `jarvis` locally (Mic/TTS) |       | - Option A: iOS Shortcut -> /ask  |
| - Daytime coding in T3 Code       |       | - Option B: Tailscale Web/Voice   |
+-----------------+-----------------+       +-----------------+-----------------+
                  |                                           |
                  | Tailscale HTTPS (:8643) + Bearer Token    |
                  +---------------------+---------------------+
                                        |
                                        v
+-------------------------------------------------------------------------------+
|                        MAC MINI (24/7 Always-On Host)                         |
|                                                                               |
|  +-------------------------------------------------------------------------+  |
|  |                     SUPERBRAIN (`~/Projects/superbrain`)                |  |
|  |  server.py (aiohttp :8643)  <--->  state.db (WAL)  <--->  reconciler.py |  |
|  +-----------------------------------------------------------------+-------+  |
|                                                                    |          |
|       spawns ephemeral `pi 1.5.0 -p --mode json` in 0700 worktrees |          |
|                                                                    v          |
|  +-------------------------------------------------------------------------+  |
|  | ~/.config/superbrain/worktrees/<task_id> (macOS sandbox-exec confined)  |  |
|  | - Fetches `origin/master` before every run                              |  |
|  | - Runs `Planner` or `Builder` (`-e ext/board.ts`)                       |  |
|  | - Runs deterministic `verify_cmd` outside `pi`                          |  |
|  | - Pushes verified branch `origin/brain/<task_id>` (never auto-merges)   |  |
|  +-------------------------------------------------------------------------+  |
+-------------------------------------------------------------------------------+
```

---

## 2. Devil's Advocate: What Breaks & How v1 Prevents It

| Failure Mode (What Breaks)                       | Counter-Measure in v1 (Proposal)                                |
| :----------------------------------------------- | :-------------------------------------------------------------- |
| MacBook vs. Mac mini git split-brain             | Every run `git fetch origin`s; verified work pushes `brain/<id>`.|
| Stray goal modifies `hamahakki` or unapproved repo| Strict `ALLOWED_REPOS` allowlist enforced in DB + `reconciler`. |
| Builder escapes worktree via `bash` or `../`     | `ext/board.ts` checks realpaths + `sandbox-exec` confines writes.|
| Prompt injection sets malicious `verify_cmd`     | Only human-approved tasks or local `Planner` set `verify_cmd`.  |
| `pi 1.5.0` `-t` flag hides `ext/board.ts` tools  | Single `-t` list explicitly includes extension tool names.      |
| Looping `opus:high` worker blows past $20 mid-run| Stream `pi --mode json` stdout; kill process on budget/timeout. |
| Weak test passes & corrupts `master` at 3 AM     | Never merge to `master` in v1; push `origin/brain/<id>` only.   |
| Plaintext port `8643` exposed on public Wi-Fi    | Bind `127.0.0.1:8643`, expose only via Tailscale + Bearer key.  |

---

## 3. Trimmed v1 Data Model (`src/superbrain/schema.sql`)

All state lives in `~/.config/superbrain/state.db` (`0600`, SQLite `WAL` mode). Instead of 8 tables, v1 uses **3 core
tables** (`tasks`, `attempts`, `outbox`) plus a 1-row `system_state` header:

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Singleton operating state (mode + daily spend circuit breaker)
CREATE TABLE system_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL DEFAULT 'ASSIST',          -- 'ASSIST' (day) | 'AUTONOMOUS' (night)
    daily_budget_usd REAL NOT NULL DEFAULT 20.0,
    spent_today_usd REAL NOT NULL DEFAULT 0.0,
    spend_date TEXT NOT NULL,                     -- YYYY-MM-DD (local time on Mac mini)
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 1. TASKS (Unified tracker for both sub-goals and problems)
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,                          -- 'SG-1', 'SG-1.1', or 'PRB-1'
    parent_id TEXT REFERENCES tasks(id),
    kind TEXT NOT NULL CHECK (kind IN ('goal', 'problem')),
    repo TEXT NOT NULL,                           -- relative path under ~/Projects, must be in ALLOWED_REPOS
    title TEXT NOT NULL,
    spec TEXT NOT NULL,                           -- hypothesis (for goal) or symptom/stacktrace (for problem)
    metric_target TEXT,                           -- required for kind='goal'
    verify_cmd TEXT,                              -- NULL = needs Planner decomposition; non-NULL = shell gate
    status TEXT NOT NULL DEFAULT 'draft',         -- 'draft' | 'ready' | 'in_progress' | 'done' | 'blocked'
    priority INT NOT NULL DEFAULT 50,             -- 0 = P0 urgent, 50 = normal, 90 = low
    strike_count INT NOT NULL DEFAULT 0,          -- 3 failed attempts -> status = 'blocked' + outbox alert
    lease_until DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 2. ATTEMPTS (Append-only anti-loop ledger across all worker runs)
CREATE TABLE attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    role TEXT NOT NULL CHECK (role IN ('planner', 'builder')),
    pi_session_id TEXT,
    session_jsonl_path TEXT,
    summary TEXT NOT NULL,
    git_branch TEXT,
    git_sha TEXT,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    outcome TEXT NOT NULL,                        -- 'pushed' | 'decomposed' | 'failed_verify' | 'deferred_approval' | 'timed_out' | 'errored'
    verify_output TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 3. OUTBOX (Pull-based digest items + binary yes/no approvals waiting on Sang)
CREATE TABLE outbox (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                           -- 'approval_request' | 'morning_brief' | 'blocker' | 'report'
    task_id TEXT REFERENCES tasks(id),
    resume_session_id TEXT,                       -- pi session ID to resume when Sang approves
    summary_spoken TEXT NOT NULL,                 -- concise 1-sentence line for Jarvis TTS
    details_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',        -- 'queued' | 'spoken' | 'approved' | 'rejected'
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

---

## 4. `pi 1.5.0` Worker Roles & `ext/board.ts`

In `pi 1.5.0` (`sdk.js:153-157`), passing `--tools` (`-t`) sets `allowedToolNames` across both built-in and extension
tools. Therefore each role passes **one combined `-t` list** that explicitly names both the allowed built-in tools and
the allowed `ext/board.ts` tools:

Common base flags for every `pi 1.5.0` run:
`-p --mode json --no-approve --no-skills --no-prompt-templates --no-mcp --no-extensions -e ext/board.ts`

| Role        | `-t` Tool Allowlist                                                      | Purpose                           |
| :---------- | :----------------------------------------------------------------------- | :-------------------------------- |
| **Planner** | `read,grep,find,ls,upsert_task,defer_for_approval`                       | Decompose goals into `tasks`      |
| **Builder** | `read,edit,write,bash,grep,find,ls,record_note,defer_for_approval`       | Fix 1 problem or build 1 goal     |

### `ext/board.ts` Tools & Guardrails

1. `upsert_task({ id?, parent_id?, kind, repo, title, spec, metric_target?, verify_cmd?, priority?, ready? })`
   * Rejects any `repo` not in `SUPERBRAIN_ALLOWED_REPOS`.
   * In `ASSIST` mode, new tasks default to `status = 'draft'` unless Sang explicitly asks to queue/run them (`ready = true`).
2. `record_note({ task_id, note_markdown })`
   * Appends structured handoff notes to `~/.config/superbrain/memory/progress/<task_id>.md`.
3. `defer_for_approval({ summary_spoken, details })`
   * Binary (`once` / `deny`) approval matching `HermesAdapter.answer()` (`src/jarvis/hermes_adapter.py:110`).
   * Writes `outbox` row with `resume_session_id = ctx.sessionManager.getSessionId()`.
   * In `ASSIST` mode during an active `/v1/runs` call, emits SSE `approval.request` immediately and waits up to 120s.
   * In `AUTONOMOUS` mode, exits cleanly with `outcome = 'deferred_approval'` so the worker slot is freed until morning.
4. `tool_call` Hook + OS Sandbox (`Builder` confinement):
   * **Path check in `ext/board.ts`:** Every `edit` and `write` call resolves `path` via `realpathSync` and blocks the
     call unless it sits inside `process.env.SUPERBRAIN_WORKTREE_DIR`. During `kind = 'problem'` runs, blocks deleting
     test files under `tests/`.
   * **macOS `sandbox-exec` profile:** `reconciler.py` spawns `Builder` inside `sandbox-exec -f worktree.sb` which
     denies file writes outside `SUPERBRAIN_WORKTREE_DIR`, `~/.config/superbrain/sessions/<task_id>`, and `/tmp`,
     making sibling repos (`~/Projects/hamahakki`, etc.) and `state.db` immutable at the kernel level.

---

## 5. Reconciler Loop (`reconciler.py`)

Every 10 seconds on the Mac mini, `reconciler.py` runs one tick:

1. **Reap Stale Leases & Reset Daily Spend:**
   * Reset `spent_today_usd = 0` when local date rolls over.
   * Any `tasks` row with `status = 'in_progress'` and `lease_until < CURRENT_TIMESTAMP` is reset to `'ready'` with an
     `'errored'` attempt logged.
2. **Budget & Mode Check:**
   * If `spent_today_usd >= daily_budget_usd` or `mode != 'AUTONOMOUS'` (unless a task was explicitly triggered by voice),
     skip background claiming.
3. **Claim Next Task (1 Worker Per Repo Max):**
   * Priority order: `kind = 'problem'` (`priority ASC`) before `kind = 'goal'` (`priority ASC`), skipping any `repo`
     that already has an `in_progress` task and any task with `strike_count >= 3`.
4. **Execute in Clean Worktree:**
   * Run `git -C ~/Projects/<repo> fetch origin --prune`.
   * If `verify_cmd IS NULL`: run **Planner** against a read-only checkout of `origin/master`.
   * If `verify_cmd IS NOT NULL`: create `~/.config/superbrain/worktrees/<task_id>` (`0700`) on branch `brain/<task_id>`
     from `origin/master` and spawn **Builder**.
   * Stream `pi --mode json` lines in real time: accumulate token cost into `spent_today_usd` and `SIGTERM`/`SIGKILL`
     immediately if the run exceeds `MAX_RUN_SECONDS` (default 900s) or `daily_budget_usd`.
5. **Independent Verification & Branch Push:**
   * After **Builder** exits, `reconciler.py` runs `verify_cmd` inside the worktree (`timeout=120s`).
   * **Exit code 0:** Commit any unstaged changes if needed, push `git push -u origin brain/<task_id>`, mark task
     `status = 'done'`, log `outcome = 'pushed'` in `attempts`, queue a `report` in `outbox`, and remove the worktree.
   * **Exit code != 0:** Increment `strike_count`, store `verify_output` in `attempts` (so the next attempt sees why it
     failed), and remove the worktree. At `strike_count == 3`, set `status = 'blocked'` and queue a `blocker` row in
     `outbox`.

---

## 6. Voice Bridge (`server.py` on Mac mini Port `8643`)

### A. 5-Endpoint `HermesAdapter` Protocol (MacBook Jarvis -> Mac mini)

Matches `src/jarvis/hermes_adapter.py` line-for-line with Bearer token auth (`Authorization: Bearer <SUPERBRAIN_API_KEY>`):

1. `POST /v1/runs` (`{"input": task, "session_id"?: id}`) -> returns `{"run_id": id}`.
   * Handles fast voice commands deterministically (0 tokens):
     * *"morning brief"* / *"what happened overnight"* -> drains queued `outbox` items into a concise summary and
       surfaces the first pending `approval_request` if any.
     * *"switch to autonomous mode"* / *"start night mode"* -> flips `system_state.mode = 'AUTONOMOUS'` and reports
       queued task count + remaining budget.
     * *"switch to assist mode"* / *"pause background work"* -> flips `system_state.mode = 'ASSIST'`.
   * Otherwise spawns **Planner** (or resumes `session_id`) to create/update `tasks` or trigger an immediate run.
2. `GET /v1/runs/{run_id}/events` -> SSE stream emitting `approval.request`, `approval.responded`, `run.completed`,
   `run.failed`, or `run.cancelled`.
3. `GET /v1/runs/{run_id}` -> JSON status fallback (`{"status": "running"|"completed"|"failed"|"cancelled", ...}`) used
   by `HermesAdapter._watch()` (`src/jarvis/hermes_adapter.py:63`) when an SSE stream drops.
4. `POST /v1/runs/{run_id}/approval` (`{"choice": "once"|"deny", "request_id"?: id}`) -> resolves the active approval
   and resumes the suspended `pi` session.
5. `POST /v1/runs/{run_id}/stop` -> aborts the active run and returns `200 OK`.

### B. iPhone Access to Mac mini

Because `jarvis` relies on macOS-only `AVAudioEngine` and `MLX Whisper`, the iPhone cannot run the `jarvis` Python CLI
locally. Two supported paths over Tailscale:

* **Option A (v1 Default — iOS Shortcut / Action Button / Siri):**
  * Add `POST /v1/ask` on `superbrain` (`server.py`): accepts `{"input": "..."}`, runs the same handler as `POST /v1/runs`
    until completion (or first approval request), and returns `{"spoken": "..."}`.
  * On iPhone (connected to Tailscale), a 3-step **iOS Shortcut** (*Dictate Text -> POST to
    `https://<mac-mini>.<tailnet>.ts.net:8643/v1/ask` -> Speak Text*) lets you talk to the Brain from your iPhone Lock
    Screen, Action Button, AirPods, or CarPlay with zero extra servers.
* **Option B (Phase 2B Optional — Full Real-Time Jarvis Voice on iPhone):**
  * Run a lightweight mobile web page / WebSocket audio bridge on the Mac mini over Tailscale HTTPS that streams iPhone
    mic/speaker audio into a headless `jarvis` pipeline on the Mac mini (`MLX Whisper` + `sherpa-onnx` voiceprint check
    + ElevenLabs TTS + `superbrain`).

---

## 7. Phased Implementation Plan (`~/Projects/superbrain`)

### Phase 1A: Private Repo Scaffold & SQLite State Store
* Create `/Users/sang/Projects/superbrain` (`git init`, `AGPL-3.0-only` `LICENSE` copied from `jarvis`, `.gitignore`,
  `pyproject.toml` with Python 3.12, `aiohttp`, `pydantic-settings`, `loguru`, `ruff`, `mypy`, `pytest`).
* Implement `src/superbrain/config.py` (`SUPERBRAIN_HOME = ~/.config/superbrain`, `ALLOWED_REPOS`, `SUPERBRAIN_API_KEY`,
  budget & timeout settings).
* Implement `src/superbrain/schema.sql` & `src/superbrain/db.py` (`tasks`, `attempts`, `outbox`, `system_state`, atomic
  lease claiming, stale-lease reaper, 3-strike transition, daily spend tracker).
* **Verification:** `uv run pytest tests/test_db.py`, `uv run ruff check .`, `uv run mypy`.

### Phase 1B: `ext/board.ts`, Sandbox, and `reconciler.py`
* Implement `src/superbrain/defaults/pi/board.ts` (`upsert_task`, `record_note`, `defer_for_approval`, and `tool_call`
  worktree realpath guard) + local unix socket RPC into `reconciler.py`.
* Implement `src/superbrain/sandbox.py` (generates the per-worktree macOS `sandbox-exec` `.sb` profile confining writes
  to the worktree and session dir).
* Implement `src/superbrain/reconciler.py` (`git fetch origin`, worktree create/cleanup, `pi 1.5.0 -p --mode json`
  streaming runner with live cost/timeout kill-switch, independent `verify_cmd` runner, `git push origin brain/<id>`).
* **Verification:** `uv run pytest tests/test_reconciler.py` (mocking `pi` JSON output and testing against a temporary
  local git repo + bare `origin` remote, verifying worktree isolation, 3-strike blocker, and budget kill-switch).

### Phase 2: Mac mini Voice Server (`server.py`), Tailscale & iPhone Bridge
* Implement `src/superbrain/server.py` (`aiohttp` server on `127.0.0.1:8643` with Bearer auth, the 5 `HermesAdapter`
  endpoints, deterministic mode/brief commands, and `POST /v1/ask` for iPhone Shortcuts).
* Implement `src/superbrain/cli.py` (`superbrain serve`, `superbrain status`, `superbrain launchd` generator for Mac mini
  `~/Library/LaunchAgents/com.sangdth.superbrain.plist`).
* **Verification:** `uv run pytest tests/test_server.py` (exercising the exact `HermesAdapter` HTTP/SSE client flow,
  drop recovery on `GET /v1/runs/{id}`, binary approval resume, and `/v1/ask`).

### Phase 3: External Sensors, Outbound Ledger, and Morning Dreamer (After 10 Clean Overnight Runs)
* Add `sensors.py` (PostHog, Gmail, Calendar, Playwright smoke checks) with untrusted triage locked out of `verify_cmd`.
* Add `outbound_ledger` + `dispatcher.py` for human-approved external actions.
* Add the 08:30 AM `Dreamer` role (`pi --export` HTML failure reports + `memory/notes/known-pitfalls.md` distillation).
