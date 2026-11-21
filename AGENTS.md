# Superbrain

An always-on goal and problem reconciler daemon (`superbrain`) that runs 24/7 on the **Mac mini** (`127.0.0.1:8643`,
exposed over Tailscale) and is controlled by **Jarvis** from the MacBook (`HermesAdapter`) or **iPhone** (`/v1/ask`
Shortcut). Plan: `docs/super-brain-plan.md`.

## Commands

- Install / sync: `uv sync`
- Run daemon: `uv run superbrain serve`
- Check status / tasks: `uv run superbrain status`
- Add or update a task: `uv run superbrain add --repo <repo> --kind goal|problem --title "..." --spec "..."`
- Run SoL-Pi auto-research hypothesis fan-out: `uv run superbrain research <task_id>`
- Run self-improving skill reflection: `uv run superbrain improve`
- Generate Mac mini `launchd` plist: `uv run superbrain launchd`
- Lint & types: `uv run ruff check src tests` and `uv run mypy`
- Tests: `uv run pytest`

## Architecture & Rules

- **Private repo, `AGPL-3.0-only` license** (`LICENSE`), matching `jarvis`.
- **State lives in `SUPERBRAIN_HOME`** (`~/.config/superbrain` by default): `.env`, `state.db` (SQLite WAL),
  `worktrees/<task_id>` (`0700`), `sessions/<task_id>`, `traces/attempt-<id>.json` (per-attempt trajectory digests),
  `skills/<slug>/SKILL.md` (capped at 10 active auto-created personal skills), and `memory/progress/<task_id>.md`
  (handoff + deterministic negative-result ledger).
- **4 SQLite tables (`schema.sql`):** `tasks` (`kind IN ('goal', 'problem')`, `best_metric`, `best_branch`, `best_sha`),
  `attempts` (append-only run ledger with `metric_value`, `skills_used`, `diagnostic`, and `reflected`), `skills`
  (versioned personal skills with baseline `sha256`, usage counters, and previous-version rollback snapshot), and
  `outbox` (queued reports, blockers, and binary approvals), plus the 1-row `system_state` table (`mode`,
  `daily_budget_usd` defaulting to `$200.00/day`, `last_good_sha`, `upgrade_runs`, `upgrade_failures`).
- **SoL-Pi auto-research & harness efficiency (`board.ts` + `reconciler.py` + `skills.py`):**
  - **Action Fusion (`edit_and_run`):** `board.ts` fuses worktree-guarded file edits with immediate follow-up test
    commands in a single turn (`[then_run:succeeded|failed|skipped]`), verifying file `sha256` stability before running.
  - **Evidence-Preserving Research Scout (`research_scout`):** Delegates read-only codebase reconnaissance to an
    isolated `scout_model` worker (`-t read,grep,find,ls`), charges its spend to `spent_today_usd`, and verifies every
    returned `{file, quote}` byte-for-byte against disk (`sol_pi_evidence_receipt_v1`) before returning it.
  - **Oracle + BFS Hypothesis Tournament (`run_auto_research`):** Computes Oracle trajectory signals (`assistant_turns`,
    `adjacent_edit_bash_pairs`, `repeated_error_tools`), fans out 2–3 competing child hypotheses, parses `METRIC: <float>`
    against `metric_target` comparators (`<=`, `<`, `>=`, `>`, `==`), tracks `best_metric`/`best_branch` on the parent
    task, and marks remaining sibling hypotheses `done` once the quantitative target is met.
  - **Sealed held-out evaluation:** Held-out baseline failure output on `repo='superbrain'` is sealed from worker-visible
    `verify_output` and progress notes to prevent test overfitting, while preserved in owner `outbox` details.
- **Self-improving skills loop (`skills.py` + `reconciler.py`):**
  - **Evidence:** `board.ts` records `record_skill_load` when a worker reads `~/.config/superbrain/skills/<slug>/SKILL.md`
    and exposes `report_diagnostic`; `reconciler.py` writes `traces/attempt-<id>.json` after every attempt.
  - **Reflection:** Offline `Improver` role receives a 1-line-per-item index of skills and trace files, inspects them on
    demand via `read`/`grep`, and proposes at most 1 change (`create`, incremental `append`, `update`, or `retire`) via
    `propose_skill_change`.
  - **Verification & Activation:** Creating a skill requires $\ge 2$ attempt IDs; `append`/`update` requires $\ge 1$
    attempt ID plus matching `expected_sha256`; at the 10-skill cap, creating requires retiring an existing skill. Any
    skill with 2 failures and 0 successes after an update automatically rolls back or retires.
- **Self-upgrading codebase (`repo = 'superbrain'`):**
  - `"superbrain"` is always in `allowed_repo_set`. When the owner asks to upgrade Superbrain or fix a Superbrain bug,
    Planner queues a `ready=true` task for `repo='superbrain'` and `server.py` executes it immediately.
  - Every `repo='superbrain'` build must pass `self_upgrade_verify_cmd` (`ruff`, `mypy`, `pytest`, and
    `superbrain self-check` canary) **plus** the pre-edit baseline `~/Projects/superbrain/tests` held-out suite against
    the candidate worktree before `~/Projects/superbrain` is updated.
  - Edits touching `pyproject.toml`, `src/superbrain/config.py`, `src/superbrain/sandbox.py`,
    `src/superbrain/defaults/pi/board.ts`, or modifying/deleting existing files under `tests/` require owner approval.
    Verified upgrades record `last_good_sha` in `system_state`, update `~/Projects/superbrain`, flush the spoken
    response, and trigger a clean shutdown for `launchd` respawn; if the first 2 post-upgrade task attempts fail with 0
    successes, Superbrain automatically rolls `~/Projects/superbrain` back to `last_good_sha` and restarts.
- **Strict repo allowlist (`ALLOWED_REPOS`):** `db.py` and `reconciler.py` refuse any `repo` not explicitly listed in
  `ALLOWED_REPOS` (comma-separated relative paths under `PROJECTS_DIR`, e.g. `jarvis,HuggerHustle/platform-v2`).
  Unlisted repos such as `hamahakki` are never touched.
- **GitHub (`origin`) is the handoff boundary between MacBook and Mac mini:**
  - Before every `Planner` or `Builder` run, `reconciler.py` runs `git -C <repo> fetch origin --prune` and creates a
    worktree from `origin/master`.
  - When a `Builder` passes `verify_cmd`, `reconciler.py` pushes `origin/brain/<task_id>` and never auto-merges into
    `master`.
- **Worktree & OS sandbox confinement (`sandbox.py` + `defaults/pi/board.ts`):**
  - `board.ts` resolves every `edit` / `write` target via `realpathSync` and blocks writes outside
    `SUPERBRAIN_WORKTREE_DIR`; on `problem` tasks, deleting test files under `tests/` is blocked.
  - On macOS, `reconciler.py` wraps `Builder` runs in `sandbox-exec -f <profile.sb>` so `bash` commands cannot write
    outside the task's worktree, session directory, or `/tmp`.
- **`pi 1.5.0` tool flag rule:** Passing `-t` sets `allowedToolNames` for both built-in and extension tools, so each
  role passes a single `-t` allowlist that explicitly includes its `board.ts` tool names (`upsert_task`,
  `record_note`, `defer_for_approval`).
- **Drop-in `HermesAdapter` + iPhone `/v1/ask` (`server.py`):**
  - Serves `POST /v1/runs`, `GET /v1/runs/{id}`, `GET /v1/runs/{id}/events`, `POST /v1/runs/{id}/approval`, and
    `POST /v1/runs/{id}/stop` with Bearer token auth (`SUPERBRAIN_API_KEY`).
  - Serves `POST /v1/ask` (`{"input": "..."}` -> `{"spoken": "..."}`) for the iPhone Shortcut / Action Button over
    Tailscale.
