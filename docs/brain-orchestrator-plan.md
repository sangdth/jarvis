# Brain as the Single Orchestrator (`voice <---> brain <---> T3 / pi`, plan, 2026-11-22)

Status: **implemented** (Phases 1–3 and 5; Phase 4's roster/prompt change is in, with the existing voice tools
kept behind the brain; the legacy coding adapters remain available only when an `agents.json` enables them).
Routing benchmark: 96% over 77 calls, 1 round, 9 cases skipped because those project folders are not on this
machine (`scripts/bench_routing.py`).

### Confirmed decisions

1. Keep Claude Code and OpenCode, minimal, behind the brain (`runners/claude.py`, `runners/opencode.py`); enrich
   them later only when needed.
2. `T3Runner` auto-starts `t3 serve --mode desktop` on loopback when nothing answers on `127.0.0.1:3773`.
3. Keep the `pi` Builder for the board and every `repo='jarvis'` self-upgrade. T3 is the human-friendly surface
   for interactive project work; more work can move into `pi` later without touching the voice or the board.
   Board tasks therefore stay `runner='pi'`; the `runner` column exists so that can change per task.

## 1. Goal & Strategic Direction

Today, orchestration is split in two:

1. `apps/voice` owns `agents.json`, `AgentOrchestrator`, project discovery (`list_projects`), session matching, and five separate agent adapters (`T3Adapter`, `ClaudeCodeAdapter`, `OpenCodeSSEAdapter`, `PiAdapter`, `HermesAdapter`).
2. `apps/brain` speaks a legacy Hermes 5-endpoint wire protocol and only knows how to spawn `pi` workers on its SQLite board — it has zero visibility into T3 Code or interactive coding sessions.

This refactor collapses the stack into one clean line:

```text
voice  <--->  brain  <--->  T3 / pi (+ minimal claude & opencode, and future tools)
```

### Core Design Choices

1. **Remove Hermes completely.** Delete `hermes_adapter.py`, `HERMES_API_KEY`, the `"hermes"` agent type, and every Hermes reference across code, config, tests, TUI, and docs.
2. **`brain` orchestrates everything.** `apps/voice` becomes pure ear, mouth, screen-eye, and conversational memory (`Mem0`); it talks exclusively to `brain` over HTTP/SSE. Consequently, `voice`, `jarvis ask`, and iPhone `/v1/ask` all share the exact same orchestrator, project resolver, and tool runners.
3. **Small tasks -> one-shot ephemeral `pi` (destroyed immediately).** Self-contained topic questions, web/docs lookups, notes, and quick inspections spawn a single-use `pi` process in a `0700` temporary directory whose session JSONL and scratch files are wiped in `finally`.
4. **Project tasks in `~/Projects/` -> long-lived T3 threads (auto-starting T3 if needed).** T3 Code is the human-friendly surface where the owner can inspect diffs, read thread history, answer prompts in GUI or by voice, and review PRs. `brain` keeps a persistent project-to-thread binding in SQLite and **auto-starts `t3 serve --mode desktop --host 127.0.0.1 --port 3773`** whenever `T3Runner` cannot connect on loopback.
5. **Keep `pi` Builder first-class; shift more work into `pi` over time.** `pi` Builder (`reconciler.py` isolated worktrees + `edit_and_run` + `verify_cmd`) stays intact for all `repo='jarvis'` self-upgrades (with `self_upgrade_verify_cmd`, sealed held-out baseline tests, and 2-run rollback probation) and headless verified `AUTONOMOUS` board builds (`runner='pi'`). Putting `T3Runner` and `PiBuilderRunner` behind one `Runner` interface in `brain` makes shifting future workloads from T3 into `pi` a policy/config switch (`DEFAULT_PROJECT_RUNNER`) with zero changes to `voice`, `cli`, or `tui`.
6. **Keep direct `claude` and `opencode` purely minimal inside `brain`.** Move `ClaudeCodeAdapter` and `OpenCodeSSEAdapter` out of `apps/voice` into `apps/brain/src/brain/runners/{claude.py,opencode.py}` as lazy, minimal pass-through runners — zero background processes or streams at startup, invoked only when the user explicitly names Claude Code or OpenCode (or runs `jarvis connect claude|opencode`), ready to be enriched later if needed.

---

## 2. Target Architecture

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ apps/voice (jarvis-voice) — Ear, Mouth, Screen & Conversational Memory       │
│   Whisper + Master Check + Laya ─▶ Executive LLM ─▶ ElevenLabs TTS           │
│   Answers small talk & general knowledge itself; sends all work to Brain     │
│   Tools: send_task · answer_question · stop_task · look_at_screen · comments │
└──────────────────────────────────────┬───────────────────────────────────────┘
                                       │ HTTP + SSE (Bearer brain_token / device token)
                                       │ POST /v1/runs · GET /v1/runs/:id/events
                                       │ GET  /v1/events · POST /v1/runs/:id/answer · /stop
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ apps/brain (brain) — Single Orchestrator, SQLite Board & Runner Host         │
│                                                                              │
│   BrainOrchestrator (0ms deterministic fast-paths + ~250ms tool router)      │
│     │                                                                        │
│     ├─ Small / topic / lookup / quick check ──▶ EphemeralPiRunner            │
│     │    • `pi -p --mode json` in 0700 TemporaryDirectory                    │
│     │    • Seatbelt write-deny outside temp dir; destroyed in `finally`      │
│     │                                                                        │
│     ├─ Project work in ~/Projects/* ──────────▶ T3Runner (default)           │
│     │    • Auto-starts `t3 serve --mode desktop` on :3773 if unreachable     │
│     │    • Multiplexed WS RPC v2; concurrent watched threads per project     │
│     │    • Relays `approval_request` & `user_input_request` verbatim         │
│     │                                                                        │
│     ├─ Explicitly named Claude / OpenCode ────▶ ClaudeRunner / OpenCodeRunner│
│     │    • Strictly lazy, minimal pass-through + `jarvis connect` support    │
│     │                                                                        │
│     ├─ Self-upgrades & headless board goals ──▶ Reconciler (`state.db`)      │
│     │    • `runner='pi'`  -> `pi` Builder in 0700 worktree + `verify_cmd`    │
│     │    • `runner='t3'`  -> future: T3 worktree thread + `verify_cmd`       │
│     │    • `Planner` / `Scout` / `Improver` stay on `pi`                     │
│     │                                                                        │
│     └─ Future tools (`Runner` registry) ──────▶ MCP, Browser, Mail, Calendar │
└──────────────────────────────────────────────────────────────────────────────┘
```

### File & Dependency Inventory

| Action | Path | Notes |
| :--- | :--- | :--- |
| **Delete** | `apps/voice/src/jarvis_voice/hermes_adapter.py` | Replaced by `brain_client.py`; Hermes removed completely |
| **Delete** | `apps/voice/tests/test_hermes_adapter.py` | Replaced by `apps/voice/tests/test_brain_client.py` |
| **Delete** | `src/jarvis/defaults/agents.json` | Routing lives in `brain`; `voice` needs no agent roster file |
| **Delete** | `apps/voice/src/jarvis_voice/{orchestrator,sessions,t3_adapter,claude_adapter,opencode_adapter,pi_adapter,agent_adapter}.py` | Moved/consolidated into `packages/jarvis-core` and `apps/brain/src/brain/runners/` |
| **New** | `packages/jarvis-core/src/jarvis_core/projects.py` | `projects_dir()`, `list_projects()`, `resolve_project()` (shared clean path resolution) |
| **New** | `packages/jarvis-core/src/jarvis_core/questions.py` | `is_approval()`, `question_fields()`, `form_answer()`, `describe_fields()`, `match_sessions()` |
| **New** | `apps/brain/src/brain/runners/{__init__,base,ephemeral_pi,t3,claude,opencode}.py` | Uniform `Runner` protocol + the 4 interactive runners |
| **New** | `apps/brain/src/brain/orchestrator.py` | Low-latency turn router, runner registry, live session/thread tracker |
| **New** | `apps/voice/src/jarvis_voice/brain_client.py` | Single HTTP/SSE client (`POST /v1/runs`, `/v1/events`, `/answer`, `/stop`, `WaitingLines`) |
| **New** | `scripts/bench_brain_routing.py` | Routing benchmark for `BrainOrchestrator` ($\ge 95\%$ gate) |
| **Modify** | `apps/brain/pyproject.toml` & `apps/voice/pyproject.toml` | Move `claude-agent-sdk>=0.2.176` from `apps/voice` to `apps/brain`; add `httpx>=0.27.0` and `websockets>=13.0` to `apps/brain` (`websockets` stays in `apps/voice` for `meet_voice.py` and `remote_audio.py`) |
| **Modify** | `apps/brain/src/brain/{config,schema.sql,db,reconciler,server}.py` | Runner settings, `threads` table & `runner` column, `/answer` & `/v1/events` endpoints |
| **Modify** | `apps/tui/src/{model,db,ui/topology}.rs` | Keep SQLite struct mapping in lockstep; update Pane 1 topology schematic |

---

## 3. Detailed Subsystem Design

### A. Complete Hermes Removal

Purge every Hermes key, type, route label, and doc reference:

1. **Settings & defaults:** Remove `hermes_api_key` from `apps/voice/src/jarvis_voice/config.py`, `HERMES_API_KEY` from `src/jarvis/defaults/env.example`, and Hermes notes in `src/jarvis/init.py`.
2. **Telemetry & server:** In `apps/brain/src/brain/server.py`, change `source="macbook_hermes"` / `source="hermes"` to `source="voice"`. Rename `test_hermes_endpoints_*` in `apps/brain/tests/test_server.py`.
3. **Cockpit & docs:** Update `apps/tui/src/ui/topology.rs` (`"Voice harness -> BrainClient"` and `"Brain HTTP/SSE/WS contract"`), `AGENTS.md`, `apps/brain/AGENTS.md`, `README.md`, `docs/architecture.md`, `docs/DECISIONS.md`, `schemas/README.md`, and `site/index.html`.

---

### B. Runner Protocol & Implementations (`apps/brain/src/brain/runners/`)

Every interactive or background execution target implements one small protocol in `apps/brain/src/brain/runners/base.py`:

```python
@dataclass(frozen=True)
class SessionInfo:
    id: str
    runner: str          # "t3" | "claude" | "opencode"
    title: str
    cwd: str
    updated_at: float
    branch: str = ""
    running: bool = False


@dataclass
class RunRequest:
    run_id: str
    prompt: str
    cwd: Path | None = None
    project: str | None = None        # relative folder under projects_dir (e.g. "HuggerHustle/platform-v2")
    session: str | None = None        # None | "new" | "<id>" | "<spoken title words>"
    pinned_session_id: str | None = None
    worktree_branch: str | None = None
    allow_live_approval: bool = True


class Runner(Protocol):
    id: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def run(self, req: RunRequest, emit: EventCallback) -> PiRunResult: ...
    async def answer(self, request_id: str, answer: str) -> bool: ...
    async def interrupt(self, run_id: str) -> bool: ...
    async def list_sessions(self, cwd: str | None = None, limit: int = 15) -> list[SessionInfo]: ...
```

#### 1. `EphemeralPiRunner` (`ephemeral_pi.py`) — One-Shot Questions & Lookups
- **Purpose:** Fast answers to topic questions, web/API lookups, reading notes/files outside a project session, or quick scratch computations without polluting `sessions/` or opening a coding thread.
- **Execution & Sandbox:**
  1. Allocates `tmp_dir = tempfile.TemporaryDirectory(prefix="brain-ephemeral-")` (`0700`).
  2. Spawns `pi -p --mode json --no-approve --no-skills --no-prompt-templates --no-mcp --no-extensions -e <board.ts> --session-dir <tmp_dir> -t read,grep,find,ls,bash,research_scout,defer_for_approval` (respecting the `pi` `-t` allowlist rule so extension tools are explicitly listed). Uses `cfg.ephemeral_model` (fallback `cfg.planner_model` / Pi default).
  3. Wraps execution in a dedicated Seatbelt profile (`sandbox.py` `build_ephemeral_sandbox_profile`):
     - Denies `file-write*` everywhere except `<tmp_dir>`, `/tmp`, `/private/tmp`, and `/dev` (zero writes to `~/Projects`, `JARVIS_HOME`, or `notes_dir`).
     - Denies `file-read*` on `~/.ssh`, `<JARVIS_HOME>/.env`, `<BRAIN_HOME>/state.db*`, and `<BRAIN_HOME>/traces`.
     - Allows outbound network so `curl` / web lookups work.
  4. Streams JSON events via `Reconciler._stream_pi_proc` with `ephemeral_timeout_seconds = 120` and live `daily_budget_usd` enforcement; records `cost_usd` via `db.add_spend()`.
  5. **Destruction guarantee:** `finally:` terminates the process group if still alive and calls `tmp_dir.cleanup()`, deleting the `.jsonl` session and all temp artifacts immediately.

#### 2. `T3Runner` (`t3.py`) — Human-Friendly Long-Lived Project Threads + Auto-Start
Ported and upgraded from `apps/voice/src/jarvis_voice/t3_adapter.py`:
- **Auto-starting T3 (`_ensure_t3_server`):**
  - Config in `brain/config.py`: `t3_url: str = "http://127.0.0.1:3773"`, `t3_auto_start: bool = True`.
  - When connecting to `ws_url` raises `OSError` / `ConnectionRefusedError` and `t3_auto_start` is `True` on a loopback host (`127.0.0.1`, `localhost`, `::1`):
    1. Acquires `self._autostart_lock` so concurrent tasks never spawn duplicate servers.
    2. Finds `t3` (`shutil.which("t3")` or `~/.t3/bin/t3`, `~/.local/bin/t3`) and spawns:
       `t3 serve --mode desktop --host 127.0.0.1 --port <port>` (`start_new_session=True`, stdout/stderr appended to `JARVIS_HOME/logs/t3-serve.log`, `0600`). `--mode desktop` shares `~/.t3/userdata` with the T3 desktop app and opens no browser window.
    3. Polls loopback readiness with backoff (100 ms -> 1 s, up to 10 s), mints a 30-day bearer token via `t3 auth session issue --scope orchestration:read --scope orchestration:operate --token-only`, and opens the WebSocket.
    4. Tracks `self._spawned_proc`: `T3Runner.stop()` terminates `self._spawned_proc` on clean shutdown, **never** killing a T3 desktop app the user started themselves.
- **Multiplexed Multi-Thread Subscriptions:**
  - Replaces `T3Adapter`'s single `_thread_id` limitation with `self._threads: dict[str, _WatchedThread]` keyed by `thread_id` (and indexed by `sub_id` on the WebSocket), so two projects can run concurrently without dropping events or approvals from either.
- **Long-Lived Project-to-Thread Binding (`threads` table in `state.db`):**
  - Resolves spoken/written project names against `projects_dir` (`~/Projects/` from `SOUL.md`) via `resolve_project()`.
  - Thread selection order for a project `P`:
    1. `req.pinned_session_id` (`jarvis connect`) -> always dispatch to that thread; never switch, never prepend `"Sync with master"`.
    2. `req.session == "new"` or `req.worktree_branch` -> launch a new thread (`orchestration.launchThread`, `generateTitle: True`, user's `~/.t3/userdata/settings.json` `defaultModelSelection` and `defaultRuntimeMode`; `workspaceStrategy: {"type": "worktree", ...}` when `worktree_branch` is set, else `{"type": "root"}`). Creates the project via `project.create` first if T3 does not know `P` yet. First message: `"Sync with the master branch first. Then: <prompt>"`.
    3. `req.session` has search words -> match against `list_sessions(cwd=P)` via `match_sessions()`, attach, and update `threads`.
    4. Default -> reuse the bound `thread_id` for `P` recorded in `state.db` `threads` if it still exists in T3 (or pick T3's newest idle user thread in `P`, never hijacking an unrelated running thread typed in the T3 UI; otherwise launch a new thread).
  - Follow-ups use `message.dispatch` (`deliveryIntent: "auto"`, `dispatchMode: {"type": "queue_after_active"}`).
- **Questions, Approvals & Interrupts (Zero-LLM):**
  - `approval_request` and `user_input_request` items emit `approval.request` / `question.request` events with `requestId` and structured `questions` directly to `BrainServer` (and `outbox` if unattended).
  - `answer()` translates the user's English reply via `is_approval()` (`accept`/`decline`) or `form_answer()` and sends `runtime-request.respond`.
  - `interrupt()` sends `run.interrupt` for active runs on the target thread.

#### 3. `PiBuilderRunner` (Existing Worktree Builder in `reconciler.py`) — Kept Intact
- Keeps all existing `Builder` logic in `apps/brain/src/brain/reconciler.py`: `0700` worktree from `origin/master`, Seatbelt + `board.ts` `realpathSync` confinement, `edit_and_run` Action Fusion, test-deletion guard on `problem` tasks, deterministic `verify_cmd` + `METRIC:` parsing, and `origin/brain/<task_id>` push.
- Always runs **`repo='jarvis'` self-upgrades** (so self-repair never depends on T3) and **`runner='pi'` board tasks** (default for unattended `AUTONOMOUS` verified builds).
- Serves as the long-term target engine as more workflows graduate from human-supervised T3 threads to autonomous `pi` execution.

#### 4. Minimal `ClaudeRunner` (`claude.py`) & `OpenCodeRunner` (`opencode.py`)
- Trimmed (~130 lines each), strictly **lazy** ports of `claude_adapter.py` and `opencode_adapter.py`:
  - `start()` does nothing at daemon boot — no SDK subprocess or SSE loop runs unless a request explicitly targets `runner="claude"` / `runner="opencode"` or `jarvis connect claude|opencode` attaches to one.
  - Supports only core `run`, `answer`, `interrupt`, and `list_sessions` (using the user's `~/.claude/settings.json` and OpenCode's `~/.local/state/opencode/service.json` / `OPENCODE_SERVER_PASSWORD`).
  - No automatic routing unless explicitly named by the user; easy to enrich later without touching `voice`.

---

### C. `BrainOrchestrator` (`apps/brain/src/brain/orchestrator.py`)

Handles every incoming interactive turn (`POST /v1/runs` and `POST /v1/ask`) with minimal latency:

1. **Tier 0 — Deterministic 0 ms Fast-Paths:**
   - `"autonomous mode"` / `"assist mode"`, `"morning brief"`, `"improve skills"`, `"run task <ID>"` (existing regex fast-paths).
   - **Pinned session (`jarvis connect`):** When the session has a `pinned` `(runner, session_id)`, `replace=True` interrupts first and every work turn dispatches straight to that runner/session with **0 ms** routing overhead.
2. **Tier 1 — Single Fast Router Call (~250 ms, OpenRouter `ORCHESTRATOR_MODEL`, `reasoning: off`):**
   - Context injected into the router prompt:
     - Listed git repos under `projects_dir` (`~/Projects/`, newest first, so Whisper mishearings like *"Hacker Hustle"* resolve cleanly to `HuggerHustle/platform-v2`),
     - Current active `(project -> thread_id, title, status)` bindings from `T3Runner` + last active runner/project in this conversation `session_id`,
     - Top board tasks & system state (`mode`, `spent_today_usd` / `daily_budget_usd`).
   - Router tools:
     - `delegate_project(project, task="", session=None, runner="t3")` — any coding, repo question, debugging, test run, screen-error fix in a project, or `"open/continue <project>"` / `"new thread"`. Defaults `runner` to `cfg.default_project_runner` (`"t3"` today); sets `runner="claude"` or `"opencode"` **only** when the user explicitly names Claude Code or OpenCode.
     - `ask_ephemeral_pi(question)` — small/self-contained topic questions, web/news/docs lookups, notes, or system checks not tied to a `~/Projects/` coding session.
     - `list_sessions(runner="t3", project=None)` — reads out open thread/session titles and status directly from the runner without starting a worker turn.
     - `queue_board_task(...)` / `run_planner(...)` — explicit multi-step / overnight `AUTONOMOUS` goals, or `repo='jarvis'` self-upgrades (which execute immediately via `PiBuilderRunner` when `ready=True` and `verify_cmd` is set, preserving today's self-upgrade UX).
     - `reply_direct(spoken)` — project-list questions (*"how many projects in OpsCom?"*) or board status questions answered immediately with zero worker spawn.

---

### D. SQLite Schema Migration & Rust Cockpit (`apps/tui`) Lockstep

Per `apps/brain/AGENTS.md`, `apps/tui/src/db.rs` reads `state.db` directly in read-only WAL mode, so schema migrations and Rust struct updates ship in the same commit:

1. **`schema.sql` & `BrainDB` migration (`apps/brain/src/brain/db.py`):**
   - `tasks`: add `runner TEXT NOT NULL DEFAULT 'pi' CHECK (runner IN ('pi', 't3', 'claude', 'opencode'))` and `thread_id TEXT`.
     - Board tasks stay `runner='pi'` (the confirmed decision): the `pi` Builder owns isolated worktrees and
       `verify_cmd`. The column exists so a specific task can choose a client later; nothing depends on that yet.
   - `attempts`: SQLite cannot `ALTER` an existing `CHECK` constraint (`role IN ('planner', 'builder', 'improver')`). Add an idempotent `_migrate_attempts_table_locked()` in `BrainDB` (`CREATE TABLE attempts_v2 ...`, copy rows, `DROP TABLE attempts`, `ALTER TABLE attempts_v2 RENAME TO attempts`) widening `role` to `('planner', 'builder', 'improver', 't3', 'ephemeral', 'claude', 'opencode')` and adding `thread_id TEXT`.
   - `threads`: new table:
     ```sql
     CREATE TABLE IF NOT EXISTS threads (
         repo TEXT PRIMARY KEY,
         runner TEXT NOT NULL DEFAULT 't3',
         thread_id TEXT NOT NULL,
         project_id TEXT,
         title TEXT NOT NULL DEFAULT '',
         worktree_path TEXT,
         branch TEXT NOT NULL DEFAULT '',
         last_sequence INTEGER,
         updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
     );
     ```
2. **Repo allowlist split (`validate_repo`):**
   - Interactive human-initiated runs (`T3Runner`, `ClaudeRunner`, `OpenCodeRunner`) accept any listed git repo under `projects_dir` (`resolve_project()`), matching today's voice UX without manual `.env` edits.
   - Unattended `AUTONOMOUS` `Reconciler` runs continue enforcing `ALLOWED_REPOS` (plus `SELF_REPOS = {'jarvis', 'brain'}`), unless `ALLOWED_REPOS="*"`.
3. **Cockpit (`apps/tui`):**
   - Update `Task` and `Attempt` in `apps/tui/src/model.rs` and queries in `apps/tui/src/db.rs` (`runner`, `thread_id`).
   - Update `apps/tui/src/ui/topology.rs` to show `Voice / CLI / iPhone -> BrainServer -> BrainOrchestrator -> {T3Runner, EphemeralPi, Reconciler (Pi Builder / Planner / Scout / Improver)}`.

---

### E. Upgraded `voice <---> brain` Wire Contract (`server.py` + `schemas/openapi.json`)

1. **`POST /v1/runs`** — accepts:
   ```json
   {
     "input": "Fix the signup form in HuggerHustle platform v2",
     "session_id": "ses-...",
     "replace": false,
     "screenshot": "/path/to/screen.png",
     "pinned": { "runner": "t3", "session_id": "thread-uuid" }
   }
   ```
   When `replace=true` and the session has an active run, `BrainServer` aborts/interrupts the in-flight run before starting the replacement.
2. **`GET /v1/runs/{run_id}/events` (SSE)** — emits:
   - `approval.request`: `{"event": "approval.request", "run_id": "...", "request_id": "...", "description": "..."}`
   - `question.request`: `{"event": "question.request", "run_id": "...", "request_id": "...", "description": "asks: ...", "questions": [...]}`
   - `session.switched`: `{"event": "session.switched", "runner": "t3", "session_id": "...", "title": "...", "project": "..."}`
   - `run.completed` / `run.failed` / `run.cancelled`
3. **`POST /v1/runs/{run_id}/answer`** (with `/v1/runs/{run_id}/approval` kept as a compatibility alias):
   - Accepts `{"answer": "<english answer>", "request_id": "..."}` and routes straight to the active runner's `answer()` — **approvals and questions never pass through an LLM**.
4. **`GET /v1/events` (persistent daemon-to-voice SSE stream):**
   - Pushes unsolicited events to `voice` when no voice-initiated HTTP run is waiting on them — e.g., a turn or approval on a `jarvis connect`-pinned T3 thread triggered from the T3 window, or a background `ASSIST` board task requesting approval / finishing.
5. **`GET /v1/sessions` & `POST /v1/connect`:**
   - Used by `jarvis connect [t3|claude|opencode]` at CLI startup (including T3 project-script auto-detection from `T3CODE_PROJECT_ROOT` and `T3CODE_WORKTREE_PATH`) to list recent threads/sessions from `brain` and return the `pinned` descriptor for the voice harness.

---

### F. Thinning `apps/voice` (`jarvis-voice`)

1. **`BrainClient` (`apps/voice/src/jarvis_voice/brain_client.py`):**
   - Replaces `AgentOrchestrator` and all 5 voice-side adapters.
   - Owns one `httpx.AsyncClient` pointed at `settings.endpoint` (`Bearer brain_token`), watches `/v1/runs/{id}/events` per run plus persistent `/v1/events`, surfaces `approval.request` / `question.request` verbatim (`is_report=False`) and finished turns via `AGENT_REPORT_PROMPT` (`is_report=True`), and drives `WaitingLines` reminders while a run is busy.
2. **Simplified Voice Tools (`apps/voice/src/jarvis_voice/agent_tools.py`):**
   - `send_task(task: str, include_screen: bool = False, replace: bool = False)` — one tool for every task, project command, session switch/listing, research question, or board request. No `agent_id` parameter; if the user names Claude Code or OpenCode, `task` simply preserves that wording (*"Using OpenCode, add dark mode"*) for `BrainOrchestrator`.
   - `answer_question(answer: str)` — answers the pending approval or question from `brain`.
   - `stop_task()` — interrupts the active run in `brain`.
   - `look_at_screen(question: str)` + `add_comment` / `read_comments` — unchanged, keeping `owner_only` and `needs_owner_ok` enforcement in meeting mode (`JarvisState.room_turn`).
3. **Simplified `JARVIS_EXECUTIVE_SYSTEM_PROMPT` (`apps/voice/src/jarvis_voice/prompts.py`):**
   - Drops multi-agent `agent_roster()` and filesystem `project_list()` from `voice` (Wait — one subtlety: keep a concise rule that questions about small talk / general knowledge are answered directly, while anything about the user's projects, sessions/threads, code, research, notes, or current info goes to `send_task` in the same turn).

---

## 4. Phased Implementation Plan & Verification Gates

Every phase leaves `just lint` and `just test` green before moving on.

### Phase 1 — Purge Hermes & Upgrade the Gateway Wire Contract
1. Remove `hermes_api_key` / `HERMES_API_KEY` and Hermes labels across `apps/voice/src/jarvis_voice/config.py`, `src/jarvis/defaults/env.example`, `src/jarvis/init.py`, and `apps/brain/src/brain/server.py`.
2. Add `/v1/runs/{id}/answer` (keeping `/approval` aliased), `question.request` SSE payloads, `GET /v1/events`, and `GET /v1/sessions` + `POST /v1/connect` in `apps/brain/src/brain/server.py` and `schemas/openapi.json`.
3. **Verify:** `uv run pytest apps/brain/tests/test_server.py`.

### Phase 2 — Shared Helpers, Schema Migration & `apps/brain` Runners
1. Extract pure project and question/session helpers into `packages/jarvis-core/src/jarvis_core/{projects,questions}.py` with unit tests.
2. Move `claude-agent-sdk` from `apps/voice/pyproject.toml` to `apps/brain/pyproject.toml`; add `httpx` and `websockets` to `apps/brain/pyproject.toml`; run `uv sync`.
3. Migrate `schema.sql` + `db.py` (`tasks.runner`, `tasks.thread_id`, widened `attempts.role` + `attempts.thread_id`, `threads` table) and update `apps/tui/src/{model,db}.rs` in the same step.
4. Implement `EphemeralPiRunner` (`ephemeral_pi.py` + `sandbox.py` ephemeral Seatbelt profile), `T3Runner` (`t3.py` with `t3 serve --mode desktop` auto-start and multi-thread subscriptions), and minimal lazy `ClaudeRunner` (`claude.py`) + `OpenCodeRunner` (`opencode.py`).
5. **Verify:** `uv run pytest apps/brain/tests` and `cargo test --workspace`.

### Phase 3 — `BrainOrchestrator` & `Reconciler` Integration
1. Implement `apps/brain/src/brain/orchestrator.py` (Tier 0 fast-paths + Tier 1 OpenRouter tool router across `T3Runner`, `EphemeralPiRunner`, minimal `ClaudeRunner`/`OpenCodeRunner`, and `Board`/`Reconciler`).
2. (Deferred, per the confirmed decision) Wire `Reconciler.execute_claimed_task` so a task that explicitly sets
   `runner='t3'` runs in a T3 worktree thread + `verify_cmd`; board work and every `repo='jarvis'` self-upgrade
   stay on the `pi` Builder.
3. Add `apps/brain/tests/test_orchestrator.py` and `scripts/bench_brain_routing.py` ($\ge 95\%$ accuracy across small questions -> ephemeral `pi`, `~/Projects/*` & misheard names -> `T3Runner`, explicit Claude/OpenCode -> minimal runners, and autonomous/self-upgrade tasks -> `Board`/`pi`).
4. **Verify:** `uv run pytest apps/brain/tests` and `uv run python scripts/bench_brain_routing.py 1`.

### Phase 4 — Collapse `apps/voice` onto `BrainClient`
1. Record baseline score on `uv run python scripts/bench_routing.py 1` before editing voice prompts/tools.
2. Implement `apps/voice/src/jarvis_voice/brain_client.py`; update `main.py`, `agent_tools.py`, `prompts.py`, and `jarvis connect` (`src/jarvis/cli.py`) to use `brain` exclusively.
3. (Superseded by the confirmed decision) Keep `t3_adapter.py`, `claude_adapter.py`, `opencode_adapter.py` and
   `pi_adapter.py` in `apps/voice` as optional adapters, loaded only when an `agents.json` enables them. The
   shipped roster is `brain` only; `hermes_adapter.py` and `src/jarvis/defaults/agents.json`'s old roster are gone.
4. Update `scripts/bench_routing.py` for the slimmed voice toolset and verify $\ge 95\%$ accuracy (English + Vietnamese cases).
5. **Verify:** `uv run pytest` and `uv run python scripts/bench_routing.py 1`.

### Phase 5 — Cockpit Topology, Docs & End-to-End Canary
1. Update `apps/tui/src/ui/topology.rs` to render the new `Voice <-> Brain <-> {T3, Ephemeral Pi, Pi Builder}` topology and run `cargo build --release --workspace`.
2. Update `AGENTS.md`, `apps/brain/AGENTS.md`, `README.md`, `docs/architecture.md`, `docs/DECISIONS.md`, `schemas/README.md`, and `site/index.html`.
3. **Final gate:** `just lint`, `just test`, and `uv run jarvis self-check`.

---

## 5. Known Risks & Guardrails

- **Auto-started `t3 serve` lifecycle vs T3 Desktop app:** Because `t3 serve --mode desktop` uses the same `~/.t3/userdata` store on `127.0.0.1:3773`, `T3Runner` only spawns `t3 serve` when `:3773` refuses TCP/WS connections, holds an `asyncio.Lock` during startup, and only terminates the process on exit if `brain` itself spawned it. If the user opens the T3 desktop app first, `brain` attaches to it seamlessly.
- **T3 provider spend is outside `pi` JSON events:** `EphemeralPiRunner`, `Planner`, `Scout`, `Improver`, and `PiBuilderRunner` report exact per-turn USD into `spent_today_usd`. T3, Claude SDK, and OpenCode bill through their own harnesses; as more project workloads shift from T3 into `pi` over time, a larger share of total spend comes automatically under `daily_budget_usd`.
- **Two-hop latency on interactive turns:** Keeping `BrainOrchestrator` as an in-process async router (`reasoning: off`, ~250 ms) instead of cold-booting a `pi` CLI subprocess for routing keeps voice-to-T3 dispatch nearly identical in latency to today's direct `T3Adapter` — and **0 ms** when pinned via `jarvis connect`.
- **Zero-LLM approval rule preserved:** Neither `voice` nor `BrainOrchestrator` ever feeds an `approval_request` or `user_input_request` from T3/Claude/OpenCode/Brain into an LLM to decide; prompts travel verbatim over SSE to TTS, and the user's answer maps deterministically via `is_approval()` / `form_answer()`.
