# Superbrain (the brain)

An always-on goal and problem reconciler. It lives in this repository as `apps/superbrain` and **always runs with
Jarvis**: `jarvis` starts it in-process on `127.0.0.1:8643` when nothing is listening there, and attaches to the
24/7 `launchd` daemon otherwise (see `src/jarvis/supervisor.py`). It is driven from the voice harness
(`HermesAdapter`, agent type `superbrain`), the cockpit (`apps/tui`), or an iPhone Shortcut over Tailscale
(`POST /v1/ask`). Plan and history: `docs/super-brain-plan.md`, `docs/architecture.md`.

## Commands

Run from the repository root. `jarvis …` is the documented surface; `uv run superbrain …` is the same code.

- Install / sync: `uv sync`
- Run the daemon: `jarvis serve` (what the LaunchAgent starts) — or simply `jarvis`, which starts it with the voice
- Check status / tasks: `jarvis status`
- Add or update a task: `jarvis add --repo <repo> --kind goal|problem --title "..." --spec "..."`
- SoL-Pi auto-research fan-out: `jarvis research <task_id>`
- Self-improving skill reflection: `jarvis improve`
- `launchd` plist: `jarvis launchd` (`--install` writes `com.sangdth.jarvis`)
- Update the checkout and restart the daemon: `jarvis upgrade` (`--check`, `--stash`, `--no-restart`, `--fetch`)
- Cockpit: `jarvis top` (Rust, `apps/tui`; build once with `cargo build --release --workspace`)
- Lint & types: `uv run ruff check apps packages tests` and `uv run mypy` (whole repo, one config)
- Tests: `uv run pytest` (this app's suite is `apps/superbrain/tests`); cockpit: `cargo test --workspace`
  and `cargo clippy --workspace --all-targets -- -D warnings`

## Architecture & Rules

- **AGPL-3.0-only**, one license for the whole repository (`/LICENSE`).
- **State lives in the brain home** (`jarvis_core.brain_home()`, `JARVIS_HOME/brain` by default; `SUPERBRAIN_HOME`
  or `JARVIS_BRAIN_HOME` overrides it, and a pre-merge `~/.config/superbrain` is read until `jarvis init` moves it):
  `.env` (shared: `JARVIS_HOME/.env` wins), `state.db` (SQLite WAL, the only writer), `worktrees/<task_id>` (`0700`),
  `sessions/<task_id>`, `traces/attempt-<id>.json`, `skills/<slug>/SKILL.md` (capped at 10 active auto-created
  personal skills), and `memory/progress/<task_id>.md` (handoff + deterministic negative-result ledger).
- **4 SQLite tables (`schema.sql`):** `tasks` (`kind IN ('goal', 'problem')`, `child_strategy IN ('all', 'any')`,
  `best_metric`, `best_branch`, `best_sha`), `attempts` (append-only run ledger with `metric_value`, `skills_used`,
  `diagnostic`, and `reflected`), `skills` (versioned personal skills with baseline `sha256`, usage counters, and
  previous-version rollback snapshot), and `outbox` (queued reports, blockers, and binary approvals), plus the 1-row
  `system_state` table (`mode`, `daily_budget_usd` defaulting to `$200.00/day`, `last_good_sha`, `upgrade_runs`,
  `upgrade_failures`). The cockpit reads these directly, read-only: a column it draws must not change shape without
  `apps/tui/src/db.rs`.
- **SoL-Pi auto-research & harness efficiency (`board.ts` + `reconciler.py` + `skills.py`):**
  - **Action Fusion (`edit_and_run`):** `board.ts` fuses worktree-guarded file mutations (`createEditToolDefinition` /
    `createWriteToolDefinition`) with immediate follow-up `createBashToolDefinition` execution in a single turn
    (`[then_run:succeeded|failed|skipped]`).
  - **Evidence-Preserving Research Scout (`research_scout`):** Delegates read-only reconnaissance to a Seatbelt-sandboxed
    `scout_model` worker (`-t read,grep,find,ls`), streams spend against `daily_budget_usd`, and verifies every returned
    `{file, quote}` byte-for-byte on disk (`sol_pi_evidence_receipt_v1`).
  - **Oracle + BFS Hypothesis Tournament (`run_auto_research`):** Computes Oracle trajectory signals (`assistant_turns`,
    `adjacent_edit_bash_pairs`, `repeated_error_tools`), fans out competing hypotheses (`child_strategy='any'`), parses
    `METRIC: <float>` against `metric_target` comparators (`<=`, `<`, `>=`, `>`, `==`), tracks `best_metric`/`best_branch`,
    and closes remaining `'any'` sibling hypotheses once the quantitative target is met.
  - **Sealed held-out evaluation:** Held-out baseline failure output on the self repository is sealed from
    worker-visible `verify_output` and progress notes to prevent test overfitting, while preserved in owner `outbox`
    details.
- **Self-improving skills loop (`skills.py` + `reconciler.py`):**
  - **Evidence:** `board.ts` records `record_skill_load` when a worker reads `skills/<slug>/SKILL.md` and exposes
    `report_diagnostic`; `reconciler.py` writes `traces/attempt-<id>.json` after every attempt.
  - **Reflection:** Offline `Improver` role receives a 1-line-per-item index of skills and trace files, inspects them on
    demand via `read`/`grep`, and proposes at most 1 change (`create`, incremental `append`, `update`, or `retire`) via
    `propose_skill_change`.
  - **Verification & Activation:** Creating a skill requires $\ge 2$ attempt IDs; `append`/`update` requires $\ge 1$
    attempt ID plus matching `expected_sha256`; at the 10-skill cap, creating requires retiring an existing skill. Any
    skill with 2 failures and 0 successes after an update automatically rolls back or retires.
- **Self-upgrading this repository (`repo = 'jarvis'`, `superbrain` still accepted):**
  - `"jarvis"` is always in `allowed_repo_set` (`config.SELF_REPO`); the checkout is `config.self_repo_dir`, the running
    one in dev (`SELF_REPO_DIR` pins another). The owner asking to upgrade "jarvis", "the brain", "your code" or
    "superbrain" queues a `ready=true` task there and `server.py` executes it immediately.
  - **Divergence-safe base:** before a self-upgrade worktree is prepared, `reconciler.py` fetches and classifies local
    `HEAD` vs `origin/master` with the pure `upgrade.classify_git_state` helper
    (`UpToDate | FastForward | LocalAhead | Diverged`). The worktree is based on `origin/master` unless local `HEAD`
    has genuinely unpublished commits ahead of it; a diverged checkout is refused with a `blocker` outbox item rather
    than resetting anything backwards.
  - Every self build must pass `self_upgrade_verify_cmd` (`ruff`, `mypy`, `pytest`, the `superbrain self-check` canary,
    then `cargo test` for the cockpit when cargo is on PATH) **plus** the pre-edit baseline held-out suite in
    `apps/superbrain/tests` against the candidate worktree before the checkout is updated.
  - Edits touching `PROTECTED_SELF_PATHS` (`pyproject.toml`, `uv.lock`, `Cargo.toml`, `Cargo.lock`,
    `apps/superbrain/src/superbrain/{config,sandbox}.py`, `apps/superbrain/src/superbrain/defaults/pi/board.ts`,
    `apps/voice/src/jarvis_voice/{config,voice_gates}.py`, `src/jarvis/{cli,supervisor}.py`), or modifying/deleting
    existing files under `tests/`, pin the verified `approved_sha` on `origin/brain/<task_id>` and require owner
    approval before that exact commit is applied. Verified upgrades record `last_good_sha` in `system_state`, update the
    checkout (running `uv sync` and, for Rust changes, `cargo build --release`), flush the spoken response, and trigger a
    clean shutdown for `launchd` respawn; probation clears `last_good_sha` once post-upgrade runs succeed, or rolls the
    checkout and `origin/master` back after 2 consecutive failures.
  - **Loud divergence handling:** `_apply_self_upgrade` refuses a diverged checkout (blocker outbox) and refuses a
    candidate that is not a descendant of the current `origin/master`; a rejected final `git push origin HEAD:master`
    is logged as an error and queued as a `report` outbox item instead of being silently ignored. `--force` is never
    used.
- **Strict repo allowlist (`ALLOWED_REPOS`):** `db.py` and `reconciler.py` refuse any `repo` not explicitly listed in
  `ALLOWED_REPOS` (comma-separated relative paths under `PROJECTS_DIR`, e.g. `HuggerHustle/platform-v2`). `jarvis` is
  always allowed; unlisted repos are never touched.
- **GitHub (`origin`) is the handoff boundary between machines:**
  - Before every `Planner` or `Builder` run, `reconciler.py` runs `git -C <repo> fetch origin --prune` and creates a
    worktree from `origin/master`; for the self repository the base is local `HEAD` only when it is genuinely ahead
    of `origin/master`.
  - When a `Builder` passes `verify_cmd`, `reconciler.py` pushes `origin/brain/<task_id>` and never auto-merges into
    `master`.
- **Worktree & OS sandbox confinement (`sandbox.py` + `defaults/pi/board.ts`):**
  - `board.ts` resolves every `edit` / `write` / `edit_and_run` target via `realpathSync`, blocks writes outside
    `SUPERBRAIN_WORKTREE_DIR`, blocks `git push` from workers, and blocks modifying/deleting tests on `problem` tasks.
  - On macOS, `reconciler.py` wraps `Planner`, `Builder`, `Improver`, and `Scout` runs in `sandbox-exec` so workers
    cannot write outside their allowed directories or read `~/.ssh`, `.env`, `state.db*`, or sealed `traces/` (except
    `Improver`, which reads `traces/`).
- **`pi` tool flag rule:** Passing `-t` sets `allowedToolNames` for both built-in and extension tools, so each
  role passes a single `-t` allowlist that explicitly includes its `board.ts` tool names (`upsert_task`,
  `record_note`, `defer_for_approval`).
- **The voice's protocol, served here (`server.py`):** `POST /v1/runs`, `GET /v1/runs/{id}`,
  `GET /v1/runs/{id}/events`, `POST /v1/runs/{id}/approval` and `POST /v1/runs/{id}/stop` with Bearer auth
  (`SUPERBRAIN_API_KEY`, else `JARVIS_HOME/brain_token`), plus `POST /v1/ask` (`{"input": "..."}` ->
  `{"spoken": "..."}`) for the iPhone Shortcut over Tailscale. The contract is `schemas/openapi.json`; keep it in step.
- **Fullscreen cockpit (`apps/tui` + `telemetry.py`):**
  - `TelemetryHub` is a dependency-free in-memory ring buffer (500 events) that fans out transient events
    (`http.inbound`, `run.status`, `approval.*`, `reconciler.phase`, `board.rpc`, `pi.progress`,
    `attempt.finished`, `heartbeat`) to SSE subscribers via `GET /v1/telemetry/events`; publishing can never fail a
    worker run.
  - `apps/tui` (`jarvis-top`, Rust + Ratatui, launched with `jarvis top`) reads `state.db` **read-only** on its own
    connection, tails `sessions/**/*.jsonl` by byte offset, and reconnects to the SSE stream automatically, so the
    dashboard survives daemon restarts and never blocks WAL writers.
  - Operator actions from the dashboard are ordinary authenticated API calls: `POST /v1/outbox/{id}/resolve`
    (approve/reject queued outbox items), `POST /v1/ask` (mode toggle, free-form instruction), and
    `POST /v1/runs/{id}/stop`. The dashboard has no write path into `state.db` or `worktrees/`.
