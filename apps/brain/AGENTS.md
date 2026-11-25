# Brain

An always-on goal and problem reconciler. It lives in this repository as `apps/brain` and **always runs with
Jarvis**: the supervisor (`src/jarvis/supervisor.py`) runs it and the voice in one process on this checkout, and its
HTTP/SSE/WebSocket gateway on `BRAIN_HOST:BRAIN_PORT` (default `0.0.0.0:8643`) is the only network surface. It is
driven from the voice harness (`BrainClientAdapter`, agent type `brain`), the dashboard (`apps/dashboard`), `jarvis` itself
(`jarvis status|add|mode|...` are HTTP calls), or an iPhone Shortcut over Tailscale (`POST /v1/ask`). Architecture and
decisions: `docs/architecture.md`, `docs/DECISIONS.md`.

## Commands

Run from the repository root. `jarvis …` is the documented surface; `uv run python -m brain.cli …` is the same code.

- Install / sync: `uv sync` (the only build step; the checkout is what runs)
- Run the whole stack: `jarvis` (this terminal) or `jarvis start` (launchd, 24/7; the same command restarts it)
- The server alone, no voice: `uv run python -m brain.cli serve` (what the LaunchAgent used to start)
- Check status / tasks: `jarvis status`, `jarvis tasks`
- Add or update a task: `jarvis add --repo <repo> --kind goal|problem --title "..." --spec "..."`
- Switch mode: `jarvis mode ASSIST|AUTONOMOUS`
- SoL-Pi auto-research fan-out: `jarvis research <task_id>`
- Self-improving skill reflection: `jarvis improve`
- `launchd` plist: `jarvis launchd` (`--install` writes it; `jarvis start` does both)
- Update the checkout and restart: `git pull` then `jarvis start` (`brain upgrade --check` reports git state)
- Dashboard: `jarvis dashboard` (Rust, `apps/dashboard`; build once with `cargo build --release --workspace`)
- Lint & types: `uv run ruff check src apps packages tests` and `uv run mypy` (whole repo, one config)
- Tests: `uv run pytest` (this app's suite is `apps/brain/tests`); dashboard: `cargo test --workspace`
  and `cargo clippy --workspace --all-targets -- -D warnings`

## Architecture & Rules

- **AGPL-3.0-only**, one license for the whole repository (`/LICENSE`).
- **State lives in the brain home** (`jarvis_core.brain_home()`, `JARVIS_HOME/brain` by default; `BRAIN_HOME`
  overrides it, and a pre-merge `~/.config/superbrain` is read until `jarvis init` moves it):
  `.env` (shared: `JARVIS_HOME/.env` wins), `state.db` (SQLite WAL, the only writer), `worktrees/<task_id>` (`0700`),
  `sessions/<task_id>`, `sessions/chat/<key>` (a resumed conversational `pi` session, pruned by age/count),
  `traces/<YYYY-MM-DD>/` (`spans.jsonl` + one `trace-<trace_id>.json`; pruned by day, see `traces.py`),
  `traces/analysis/` (`overview.md` + `surfaces/<surface>.md`, the digest the Improver reads first),
  `skills/<slug>/SKILL.md` (capped at 10 active auto-created personal skills),
  `INSTRUCTIONS.md` (owner-written, read-only to workers), and `memory/` (`MEMORY.md`, the small injected index;
  `repos/<slug>.md`; `reported.md`, what the voice has taken; `bookmarks.json`; and `progress/<task_id>.md`, the
  handoff + verified/negative-result ledger).
- **9 SQLite tables (`schema.sql`):** `tasks` (`kind IN ('goal', 'problem')`, `mode IN ('ASSIST', 'AUTONOMOUS')`,
  `child_strategy IN ('all', 'any')`, `runner IN ('pi', 't3', 'claude', 'opencode')` defaulting to `pi`,
  `thread_id`, `dedupe_key` for idempotent timer/webhook submissions, `best_metric`, `best_branch`, `best_sha`),
  `attempts` (append-only run ledger with `thread_id`, `metric_value`, `skills_used`, `diagnostic`, and `reflected`),
  `threads` (one bound long-lived client thread per repo: `runner`, `thread_id`, `project_id`, `title`,
  `worktree_path`, `branch`, `last_sequence`), `skills` (versioned personal skills with baseline `sha256`, usage
  counters, and previous-version rollback snapshot), `outbox` (queued reports, blockers, and binary approvals),
  `timers` (one-shot `followup` or five-field cron `schedule`, `action='task'|'check'`, `next_run_at` in UTC and
  `timezone` for the cron), `trace_runs` (one row per voice turn, interactive run or board attempt: `surface`,
  `outcome`, `friction_kind`, `causal_sig`, `git_sha`, `prompt_sha`, `cost_usd`, `trace_path`) with its `trace_fts`
  FTS5 index, `harness_proposals` (one row per Improver harness fix: `surface`, `spec_hash`, the measured
  `baseline_fail_rate`, and `outcome IN ('proposed','pushed','failed_verify','rejected','rolled_back','falsified','verified')`),
  plus the 1-row `system_state` table (`mode`, `daily_budget_usd` defaulting to
  `$200.00/day`, `last_good_sha`, `upgrade_runs`, `upgrade_failures`, and the Improver's 7-day probation columns). The
  reconciler loop runs continuously in both modes, resolving ready `ASSIST` tasks when in `ASSIST` mode and ready
  `AUTONOMOUS` tasks when in `AUTONOMOUS` mode. When the owner gives a new request clearly related to the current
  topic or active tasks, the Planner sets `mode='ASSIST'` and `ready=true`; if unsure when to resolve it, the
  Planner asks the owner first. In `ASSIST` too, once a day after `nightly_reflect_hour` (03:00 local) with at
  least 5 unreflected traces, the reconciler runs the Improver over the durable digest; harness proposals still
  land as `draft` unless `AUTONOMOUS_HARNESS_UPGRADES=true`. The dashboard reads these tables directly, read-only:
  a column it draws must not change shape without `apps/dashboard/src/db.rs`.
- **One orchestrator, several runners (`orchestrator.py` + `runners/`):**
  - `BrainOrchestrator.route` turns one interactive request into one runner call: a `Mode`/brief/improve/task
    command is handled in code first; a pinned `jarvis connect` session bypasses the router; otherwise one cheap
    OpenRouter tool call (thinking off) picks `delegate_project`, `ask_ephemeral_pi`, `list_sessions`,
    `queue_board_task` or `reply_direct`, and a local rule stands in when the router is unavailable.
  - `EphemeralPiRunner`: one `pi` process with `read,grep,find,ls,bash`; a question with no `session_key` uses
    a fresh `0700` temp dir and leaves nothing behind, while a voiced follow-up keeps `sessions/chat/<key>` and
    resumes the same transcript (pruned by age/count). Seatbelt writes stay in that dir, and both the daily budget
    and `max_ephemeral_cost_usd` can kill the process.
  - `T3Runner`: one WebSocket (protocol 2) carrying every watched thread; binds a repo to a thread in `threads`;
    starts `t3 serve --mode desktop` on loopback when nothing answers; relays `approval.request` /
    `question.request` verbatim and answers with `runtime-request.respond` (deterministic mapping, no LLM).
  - `ClaudeRunner` / `OpenCodeRunner`: deliberately minimal, lazy one-shot clients, used only when named.
  - Board work stays with the `pi` Builder: isolated worktree, `edit_and_run`, `verify_cmd`, held-out tests and
    self-upgrade rollback are unchanged.
- **Timers, outbox delivery and durable memory (`schedule.py`, `memory.py`, `db.py`, `reconciler.py`, `server.py`):**
  - **Timers:** `GET|POST /v1/timers`, `POST /v1/timers/{id}/cancel`; `jarvis remind "..." in 30m`,
    `jarvis schedule --every "0 8 * * 1-5" ...`, `jarvis timers`. `Reconciler.tick` calls `fire_due_timers`, which
    atomically marks a due row `firing` and advances a cron schedule to its next slot (a missed slot is skipped,
    never replayed in a burst). `action='task'` queues a board task under `dedupe_key=timer:<id>:<slot>`;
    `action='check'` runs a short read-only `pi` turn beside the loop, queues the answer to the outbox, and writes
    `BOOKMARK <source>=<cursor>` lines to `memory/bookmarks.json` instead of speaking them. A crash between firing
    and finishing is recovered by `recover_firing_timers`.
  - **Outbox delivery:** in `ASSIST` mode `BrainDB.queue_outbox` publishes `outbox.queued` on `GET /v1/events`; the
    voice (`BrainClientAdapter._events_loop`) speaks it and acknowledges `POST /v1/outbox/{id}/spoken`, which writes
    one line to `memory/reported.md`. In `AUTONOMOUS` mode nothing is spoken until the owner switches back or asks
    for the brief. A reconnecting voice drains `GET /v1/outbox?status=queued` first.
  - **Memory:** every worker gets `INSTRUCTIONS.md` and `memory/MEMORY.md` through repeated
    `--append-system-prompt` (prefix-stable, so prompt caching stays warm). `MEMORY.md` is capped at 150 lines and
    repo notes at 80; only the offline `Improver` writes them, through `propose_memory_update`, with an
    `expected_sha256` check. Progress notes record **verified** results as well as failures; the reflection prompt
    is the "dreaming" pass that corrects stale notes against traces.
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
- **Self-improving skills loop (`skills.py` + `traces.py` + `reconciler.py`):**
  - **Evidence:** `board.ts` records `record_skill_load` when a worker reads `skills/<slug>/SKILL.md` and exposes
    `report_diagnostic`; `reconciler.py` writes `traces/attempt-<id>.json` after every attempt. `traces.py`'s
    `TraceSink` drains `TelemetryHub` into `traces/<YYYY-MM-DD>/` and `trace_runs`, stamping `git_sha`/`prompt_sha`
    and the deterministic friction signal (`replaced`, `stopped`, `router_override`, `not_you`, `asr_rewrite`,
    `latency_sla`, …); pairing codes, credentials and room speech never reach disk.
  - **Reflection:** `distill_experience` writes `traces/analysis/overview.md` (health per commit, top causal
    clusters, failed-then-passed pairs) and `surfaces/<surface>.md`; the offline `Improver` reads that first, then
    drills into the `trace-*.json` it cites. It also receives a 1-line-per-item index of skills, repo notes and
    attempt traces and the deterministic failure summary (`skills.mine_failure_patterns`, plus
    `feedback/intent.jsonl` corrections), and proposes at most 1 skill
    change (`create`, incremental `append`, `update`, or `retire`) via `propose_skill_change`, at most 1 memory
    change via `propose_memory_update` (checking the claim against the trace first), or at most 1 bounded harness
    fix via `propose_harness_task` (evidence + reproduce-first `verify_cmd`; a `repo='jarvis'` `draft` task under
    `harness:<surface>`, `ready` only with `AUTONOMOUS_HARNESS_UPGRADES`). Protected files are refused.
  - **Verification & Activation:** Creating a skill requires $\ge 2$ attempt IDs; `append`/`update` requires $\ge 1$
    attempt ID plus matching `expected_sha256`; at the 10-skill cap, creating requires retiring an existing skill. Any
    skill with 2 failures and 0 successes after an update automatically rolls back or retires.
- **Self-upgrading this repository (`repo = 'jarvis'`, `brain` still accepted):**
  - `"jarvis"` and `"brain"` are always in `allowed_repo_set` (`config.SELF_REPOS`); the checkout is `config.self_repo_dir`, the running
    one in dev (`SELF_REPO_DIR` pins another). Asking to upgrade "jarvis", "the brain", "your code", "brain" or
    "your dashboard" (`apps/dashboard/`) queues a `ready=true` task there and `server.py` runs it immediately.
  - **Divergence-safe base:** before a self-upgrade worktree is prepared, `reconciler.py` fetches and classifies local
    `HEAD` vs `origin/master` with the pure `upgrade.classify_git_state` helper
    (`UpToDate | FastForward | LocalAhead | Diverged`). The worktree is based on `origin/master` unless local `HEAD`
    has genuinely unpublished commits ahead of it; a diverged checkout is refused with a `blocker` outbox item rather
    than resetting anything backwards.
  - Every self build must pass `self_upgrade_verify_cmd` (`ruff`, `mypy`, `pytest`, `brain self-check`, plus `cargo test`
    for the dashboard when cargo is on PATH) **plus** the pre-edit baseline suites (`tests/`, `apps/*/tests/`) against
    the candidate worktree. Voice and router edits also run `scripts/bench_intent.py 1` (≥97%),
    `scripts/bench_routing.py 1` (≥95%, `jarvis_voice/{prompts,agent_tools,orchestrator}.py`),
    `scripts/bench_voice_prompts.py --split held_in`, and `scripts/bench_brain_route.py 1 --split held_in` (≥95%,
    p50 ≤ 800 ms, `brain/orchestrator.py`), plus sealed `evals/held_out/` regression and Pareto checks from the checkout.
  - Edits touching `PROTECTED_SELF_PATHS` (`pyproject.toml`, `uv.lock`, `Cargo.toml`, `Cargo.lock`,
    `apps/brain/src/brain/{config,sandbox,traces,evals}.py`, `apps/brain/src/brain/defaults/pi/board.ts`,
    `apps/voice/src/jarvis_voice/{config,voice_gates}.py`, `src/jarvis/{cli,supervisor}.py`, `scripts/bench_*.py`), or
    modifying/deleting existing files under `tests/`, pin `approved_sha` on `origin/brain/<task_id>` and require owner
    approval before applying. Verified upgrades record `last_good_sha` in `system_state`, update the checkout (`uv sync`
    and `cargo build --release` when Rust changed), flush speech, and shut down cleanly for `launchd` respawn; probation
    clears `last_good_sha` once post-upgrade runs succeed, or rolls back after 2 consecutive failures.
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
  `POST /v1/runs/{id}/answer` (`{"answer": "..."}`, with `/approval` kept for binary callers),
  `POST /v1/runs/{id}/stop`, `GET /v1/sessions`, `POST /v1/connect` and `GET /v1/events` with Bearer auth
  (`BRAIN_API_KEY`, else `JARVIS_HOME/brain_token`), plus `POST /v1/ask` (`{"input": "..."}` -> `{"spoken": "..."}`)
  for the iPhone Shortcut over Tailscale; the operator routes `/v1/status`, `/v1/board`, `GET|POST /v1/tasks`,
  `/v1/tasks/{id}/research`, `/v1/mode`, `/v1/skills/improve`, `/v1/traces`, `/v1/traces/diff`,
  `/v1/traces/{id}`, `/v1/restart`; `/v1/pair*`, `/v1/devices*`, `/v1/telemetry/*`; and the remote client's `/ws`,
  `/client.py`, `/Jarvis.app.zip`, `/install-app` (`jarvis_voice/remote_audio.py`). Only `/health` and the client
  bootstrap (`/client.py`, `/Jarvis.app.zip`, `/install-app`, `POST /v1/pair/request`, `POST /v1/pair`) are open without
  a token; paired device secrets work only on `GET /v1/status` and `/ws`, and every other route requires the owner token.
  Any authenticated route accepts `Authorization: Bearer`, `?token=`, or `X-Jarvis-Token` (`schemas/openapi.json`).
- **Fullscreen dashboard (`apps/dashboard` + `telemetry.py`):**
  - `TelemetryHub` is a dependency-free in-memory ring buffer (2000 events) that fans out transient events
    (`http.inbound`, `run.status`, `approval.*`, `reconciler.phase`, `board.rpc`, `pi.progress`,
    `attempt.finished`, `heartbeat`, `pair.*`, `brain.route`) plus the voice's turn stages (`voice.stt`,
    `voice.intent`, `voice.cleanup`, `voice.memory`, `voice.request`, `voice.thinking`, `voice.tool`,
    `voice.dispatch`, `voice.return`, `voice.retell`, `voice.tts`, all grouped by `turn_id`) to SSE subscribers via
    `GET /v1/telemetry/events`; publishing can never fail a worker run. The voice's half is
    `apps/voice/src/jarvis_voice/telemetry.py`, bound to this hub by `src/jarvis/supervisor.py` and a no-op when the
    voice runs alone.
  - `apps/dashboard` (`jarvis-dashboard`, Rust + Ratatui, launched with `jarvis dashboard`) reads `state.db` **read-only** on its own
    connection, tails `sessions/**/*.jsonl` by byte offset, and reconnects to the SSE stream automatically, so the
    dashboard survives daemon restarts and never blocks WAL writers.
  - Operator actions from the dashboard are ordinary authenticated API calls: `POST /v1/outbox/{id}/resolve`
    (approve/reject queued outbox items), `POST /v1/ask` (mode toggle, free-form instruction), and
    `POST /v1/runs/{id}/stop`. The dashboard has no write path into `state.db` or `worktrees/`.
