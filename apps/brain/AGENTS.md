# Brain

An always-on goal and problem reconciler. It lives in this repository as `apps/brain` and **always runs with
Jarvis**: the supervisor (`src/jarvis/supervisor.py`) runs it and the voice in one process on this checkout, and its
HTTP/SSE/WebSocket gateway on `BRAIN_HOST:BRAIN_PORT` (default `0.0.0.0:8643`) is the only network surface. It is
driven from the voice harness (`HermesAdapter`, agent type `brain`), the cockpit (`apps/tui`), `jarvis` itself
(`jarvis status|add|mode|...` are HTTP calls), or an iPhone Shortcut over Tailscale (`POST /v1/ask`). Plan and
history: `docs/super-brain-plan.md`, `docs/architecture.md`.

## Commands

Run from the repository root. `jarvis …` is the documented surface; `uv run python -m brain.cli …` is the same code.

- Install / sync: `uv sync` (the only build step; the checkout is what runs)
- Run the whole stack: `jarvis` (this terminal) or `jarvis start` (launchd, 24/7)
- Restart after an edit: `jarvis restart`
- The server alone, no voice: `uv run python -m brain.cli serve` (what the LaunchAgent used to start)
- Check status / tasks: `jarvis status`, `jarvis tasks`
- Add or update a task: `jarvis add --repo <repo> --kind goal|problem --title "..." --spec "..."`
- Switch mode: `jarvis mode ASSIST|AUTONOMOUS`
- SoL-Pi auto-research fan-out: `jarvis research <task_id>`
- Self-improving skill reflection: `jarvis improve`
- `launchd` plist: `jarvis launchd` (`--install` writes it; `jarvis start` does both)
- Update the checkout and restart: `git pull` then `jarvis restart` (`brain upgrade --check` reports git state)
- Cockpit: `jarvis top` (Rust, `apps/tui`; build once with `cargo build --release --workspace`)
- Lint & types: `uv run ruff check src apps packages tests` and `uv run mypy` (whole repo, one config)
- Tests: `uv run pytest` (this app's suite is `apps/brain/tests`); cockpit: `cargo test --workspace`
  and `cargo clippy --workspace --all-targets -- -D warnings`

## Architecture & Rules

- **AGPL-3.0-only**, one license for the whole repository (`/LICENSE`).
- **State lives in the brain home** (`jarvis_core.brain_home()`, `JARVIS_HOME/brain` by default; `BRAIN_HOME`
  overrides it, and a pre-merge `~/.config/superbrain` is read until `jarvis init` moves it):
  `.env` (shared: `JARVIS_HOME/.env` wins), `state.db` (SQLite WAL, the only writer), `worktrees/<task_id>` (`0700`),
  `sessions/<task_id>`, `traces/attempt-<id>.json`, `skills/<slug>/SKILL.md` (capped at 10 active auto-created
  personal skills), and `memory/progress/<task_id>.md` (handoff + deterministic negative-result ledger).
- **4 SQLite tables (`schema.sql`):** `tasks` (`kind IN ('goal', 'problem')`, `mode IN ('ASSIST', 'AUTONOMOUS')`,
  `child_strategy IN ('all', 'any')`, `best_metric`, `best_branch`, `best_sha`), `attempts` (append-only run ledger
  with `metric_value`, `skills_used`, `diagnostic`, and `reflected`), `skills` (versioned personal skills with baseline
  `sha256`, usage counters, and previous-version rollback snapshot), and `outbox` (queued reports, blockers, and binary
  approvals), plus the 1-row `system_state` table (`mode`, `daily_budget_usd` defaulting to `$200.00/day`,
  `last_good_sha`, `upgrade_runs`, `upgrade_failures`). The reconciler loop runs continuously in both modes, resolving
  ready `ASSIST` tasks when in `ASSIST` mode and ready `AUTONOMOUS` tasks when in `AUTONOMOUS` mode. When the owner
  gives a new request clearly related to the current topic or active tasks, the Planner sets `mode='ASSIST'` and
  `ready=true`; if unsure when to resolve it, the Planner asks the owner first. The cockpit reads these tables
  directly, read-only: a column it draws must not change shape without `apps/tui/src/db.rs`.
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
- **Self-upgrading this repository (`repo = 'jarvis'`, `brain` still accepted):**
  - `"jarvis"` and `"brain"` are always in `allowed_repo_set` (`config.SELF_REPOS`); the checkout is `config.self_repo_dir`, the running
    one in dev (`SELF_REPO_DIR` pins another). The owner asking to upgrade "jarvis", "the brain", "your code" or
    "brain" queues a `ready=true` task there and `server.py` executes it immediately.
  - **Divergence-safe base:** before a self-upgrade worktree is prepared, `reconciler.py` fetches and classifies local
    `HEAD` vs `origin/master` with the pure `upgrade.classify_git_state` helper
    (`UpToDate | FastForward | LocalAhead | Diverged`). The worktree is based on `origin/master` unless local `HEAD`
    has genuinely unpublished commits ahead of it; a diverged checkout is refused with a `blocker` outbox item rather
    than resetting anything backwards.
  - Every self build must pass `self_upgrade_verify_cmd` (`ruff`, `mypy`, `pytest`, the `brain self-check` canary,
    then `cargo test` for the cockpit when cargo is on PATH) **plus** the pre-edit baseline held-out suite in
    `apps/brain/tests` against the candidate worktree before the checkout is updated.
  - Edits touching `PROTECTED_SELF_PATHS` (`pyproject.toml`, `uv.lock`, `Cargo.toml`, `Cargo.lock`,
    `apps/brain/src/brain/{config,sandbox}.py`, `apps/brain/src/brain/defaults/pi/board.ts`,
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
    `BRAIN_WORKTREE_DIR`, blocks `git push` from workers, and blocks modifying/deleting tests on `problem` tasks.
  - On macOS, `reconciler.py` wraps `Planner`, `Builder`, `Improver`, and `Scout` runs in `sandbox-exec` so workers
    cannot write outside their allowed directories or read `~/.ssh`, `.env`, `state.db*`, or sealed `traces/` (except
    `Improver`, which reads `traces/`).
- **`pi` tool flag rule:** Passing `-t` sets `allowedToolNames` for both built-in and extension tools, so each
  role passes a single `-t` allowlist that explicitly includes its `board.ts` tool names (`upsert_task`,
  `record_note`, `defer_for_approval`).
- **The gateway, served here (`server.py`):** one aiohttp app on `BRAIN_HOST:BRAIN_PORT` (default
  `0.0.0.0:8643`). `POST /v1/runs`, `GET /v1/runs/{id}`, `GET /v1/runs/{id}/events`,
  `POST /v1/runs/{id}/approval` and `POST /v1/runs/{id}/stop` with Bearer auth (`BRAIN_API_KEY`, else
  `JARVIS_HOME/brain_token`), plus `POST /v1/ask` (`{"input": "..."}` -> `{"spoken": "..."}`) for the iPhone
  Shortcut over Tailscale; the operator routes `/v1/status`, `/v1/board`, `GET|POST /v1/tasks`,
  `/v1/tasks/{id}/research`, `/v1/mode`, `/v1/skills/improve`, `/v1/restart`; the cockpit's telemetry; and the thin
  client's `/ws` + `/client.py` (registered from `jarvis_voice/remote_audio.py`). `/health` is the only route
  without a token. Any route also accepts `?token=` or `X-Jarvis-Token`. The contract is `schemas/openapi.json`;
  keep it in step.
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
