# Jarvis

One repository, three apps. The **voice layer** (`jarvis-voice`, Pipecat) talks to the user and hands work to
agents: coding to T3 Code (or Claude Code or OpenCode when named), everything else to **the brain** (`brain`), the daemon
that always runs with it. The **cockpit** (`jarvis-top`, Rust + Ratatui) shows the brain's board live. No wake word.
Jarvis stays thin: it routes and relays; the work, tools and memory live in each app's or agent's own harness.
Architecture: `docs/architecture.md`. Choices with known limits: `docs/DECISIONS.md`. Setup and voice commands:
`README.md`.

## Commands

- Production is a checkout, not a package: `curl -fsSL https://sangdth.github.io/jarvis/install.sh | sh`
  (`site/install.sh`: uv, clone into `~/Projects/jarvis`, `uv sync`, a thin `~/.local/bin/jarvis` shim into that
  `.venv`, then `jarvis init`). The checkout *is* the runtime: an edit plus `jarvis start` is live, and so is
  `git pull` plus `jarvis start`. `jarvis --version` names the running branch and commit (`+dirty` when edited).
  Commands: `jarvis` (this Mac's mic and speaker, or a paired Mac's when it connects), `jarvis serve [--meeting|--connect X]`
  (the same, spelled out), `jarvis start|stop|logs` (the 24/7 launchd service on this checkout, and the one command
  that restarts it), `jarvis pair`
  (one code, then another Mac's mic and speaker take over while connected: `jarvis remote` is gone), `jarvis meeting`
  (`--meeting` after `connect`), `jarvis connect` (continue a T3 thread; as a T3
  project script, the thread it runs in), `jarvis connect claude|opencode|pi`, `jarvis health|status|board|tasks|add|mode|research|improve|traces|timers|remind|schedule`
  (HTTP calls to the running server, `--url` for another machine), `jarvis top`, `jarvis init`, `jarvis enroll`.
  One listens at a time (`hold_lock`, `~/.config/jarvis/jarvis.lock`, which stays machine-wide so two checkouts
  cannot share one mic).
- Needs: nothing extra for the brain (it runs with `jarvis`); T3 Code on loopback (the brain starts
  `t3 serve --mode desktop` itself when nothing answers on `127.0.0.1:3773`); OpenCode 2's service only when
  the owner names OpenCode.
- Test: `uv run pytest` (the CLI, the voice harness and the brain; config in `pyproject.toml`), and
  `cargo test --workspace` for the cockpit. `just test` runs both, `just lint` runs ruff, mypy and clippy.
- Lint and types: `uv run ruff check src apps packages tests scripts`, `uv run mypy`, and
  `cargo clippy --workspace --all-targets -- -D warnings`
- Build the cockpit: `cargo build --release --workspace` (binary `target/release/jarvis-top`, `jarvis top` finds it)
- Speech benchmark: `uv run python scripts/bench_intent.py` (paid OpenRouter calls, about 150 at 3 rounds)
- Routing benchmark: `uv run python scripts/bench_routing.py` (about 240 paid calls; `bench_routing.py 1` runs one round, 80)
- Landing page: `site/index.html`, one static file; `.github/workflows/pages.yml` publishes it to
  <https://sangdth.github.io/jarvis/> on every push to master that touches `site/`.

## Repository layout

| Path | What lives there |
| :--- | :--------------- |
| `apps/voice/src/jarvis_voice/` | the voice layer (`jarvis-voice`): ear, speech check, executive model, agent watchers |
| `apps/brain/src/brain/` | the brain (`brain`): board, reconciler, `pi` workers, HTTP/SSE server |
| `apps/tui/` | the cockpit (`jarvis-top`, Rust + Ratatui) |
| `apps/connect/` | the Connect app (`Jarvis.app`, SwiftUI): one Connect button, served at `/Jarvis.app.zip` |
| `packages/jarvis-core/` | what all three share: `REPO_ROOT`/`JARVIS_HOME`, the `.env` settings base, the tokens, paired devices, `SOUL.md` |
| `src/jarvis/` | the `jarvis` command: dispatch, `init`, `top`, the launchd service, and the supervisor that runs the stack |
| `schemas/` | cross-language contracts (the gateway's HTTP/SSE/WS API, see `schemas/README.md`) |

Each language keeps its native workspace (`pyproject.toml` + `uv.lock`, `Cargo.toml` + `Cargo.lock`, and
`pnpm-workspace.yaml`/`go.work` when a React client or a Go service arrives). `justfile` is the one task runner,
`mise.toml` pins the toolchains. A new app is a folder under `apps/` and one line in the workspace, not a new repo.

## Where to fix "Jarvis"

Start from what the user heard or didn't. The obvious file is often wrong: "Jarvis ignores me" is usually the
speech check dropping the sentence, not the conversation model. Paths in the table are inside
`apps/voice/src/jarvis_voice/` unless they name another app.

| Symptom                              | Where                                                           |
| :----------------------------------- | :-------------------------------------------------------------- |
| Doesn't react to something said      | `executive_filter.py` `INTENT_QUESTION`, Laya `jarvis-intent`   |
| Mute / unmute phrase missed          | `state.py` `_MUTE_PATTERNS`, `_UNMUTE_PATTERNS`                 |
| Which model answered, corrections    | `~/.config/jarvis/feedback/intent.jsonl`, `feedback.py`         |
| Heard right, reached the model wrong | `[Cleanup]` log, `prompts.py` `CLEANUP_PROMPT`, `cleanup.py`  |
| Reacts, but the reply is wrong       | `prompts.py` `JARVIS_EXECUTIVE_SYSTEM_PROMPT`, `languages.json` |
| Says the user's name too often       | `prompts.py` rule 9, `soul.py` `render_soul`, user's `SOUL.md`  |
| Lists sessions wrong, no titles      | `agent_tools.py` `list_sessions`, `prompts.py` rule 10          |
| Says something wrong about an agent  | `*_adapter.py` `_surface` call sites                            |
| Retold agent turn is long or odd     | `prompts.py` `AGENT_REPORT_PROMPT`, `state.py` `_retell`        |
| Wrong agent gets a task              | `agents.json` `role`, `prompts.py` `agent_roster`               |
| Brain runs nothing overnight         | `apps/brain/src/brain/reconciler.py`, `DEFAULT_MODE`    |
| Brain spends too much, or stops      | `db.py` budget breaker, `config.py` `max_*_cost_usd`, `reconciler.py` `_run_pi` |
| Cockpit shows nothing / no data      | `apps/tui/src/db.rs`, `telemetry.py`, `jarvis top`              |
| A reminder or scheduled check never fires | `schedule.py`, `db.py` `fire_due_timers`, `reconciler.py` `_fire_timers` |
| Work finished but Jarvis said nothing | `server.py` `_deliver_outbox`, `brain_client.py` `_events_loop` |
| A worker repeats itself or forgets a verified fact | `memory.py`, `reconciler.py` `_finalize_attempt_artifacts`, `run_skill_improver` |
| `jarvis` starts a second brain       | `src/jarvis/cli.py` (`hold_lock`: one mic, one brain)      |
| Answer or stop hits the wrong agent  | `brain_server.py` `pending_approval_future`, each runner `interrupt` |
| A correction runs as a second task   | `agent_tools.py` `send` (`replace`), `server.py` `handle_create_run` |
| "Still waiting" timing or wording    | `remind_while_busy`, `waiting_lines.py`, `WAITING_PROMPT`       |
| Brain runs, approvals, stop          | `brain_client.py` `BrainClientAdapter` (type `brain`), `server.py` |
| Which runner a turn takes            | `apps/brain/src/brain/orchestrator.py` `route`, `runners/`      |
| T3 Code threads, approvals, stop     | `apps/brain/src/brain/runners/t3.py` (auto-starts `t3 serve`)   |
| `jarvis connect <agent>`, sessions   | `sessions.py` `CodingSessionAdapter`, `main.py` `pick_session`  |
| Pi windows                           | `pi_adapter.py` `PiAdapter`, `defaults/pi/jarvis.ts`            |
| Beeps, muted queue, unmute digest    | `state.py` `JarvisState`                                        |
| Mic, Whisper, interruptions          | `main.py` pipeline, `voice_gates.py`                            |
| Hears itself through the speakers    | `mac_audio.py` `MacVoiceAudio` (macOS voice processing)         |
| Hears nothing, "Mic is silent" log   | Output is Multi-Output; `mac_audio.py` `SILENT_MIC_WARNING`     |
| Other Mac: no sound, or not heard    | `remote_audio.py` (token, one client), `mac_audio.py` (handoff), `defaults/remote/client.py` |
| The mac app: no mic, or won't connect | `apps/connect/src/AudioRelay.swift` (prime, 1013), `ApiClient.swift`, `remote_audio.py` `/ws` |
| Answers on the wrong Mac             | `remote_audio.py` `Ear.accept` (`SPEECH_PEAK`, `HANDOVER_SECS`), `[Ear]` lines |
| Meeting: Jarvis can't hear the call  | `mac_audio.py` `MeetingAudioBridge`, `TAP_FAILED` log           |
| Meeting: the call can't hear Jarvis  | `meet_voice.py` `MeetVoice`, `defaults/meet/jarvis.js`          |
| Drops my voice / lets others in      | `[Speaker] score=` log lines, `SPEAKER_THRESHOLD`               |
| A guest is unnamed or misnamed       | `speaker.py` `SpeakerBeliefTracker`, `voice_gates.py` `_speaker_tag` |
| Cut off, or splits one sentence      | `SMART_TURN`, `SpeakerVerifiedWhisperSTT`, `VADParams`          |
| Slow reply: where the time goes      | `[Latency]` and `[Intent] ... ms` log lines, `latency.py`       |
| Screen: unseen, or looked unasked    | `screen.py`, `look_at_screen`, `include_screen`, prompt rule 8  |
| Voice sound, laughs, emotion         | `main.py` `ElevenLabsDialogueTTSService`, `expression_policy`   |
| Forgets, remembers wrong, slow reply | `memory.py` (Mem0, Qdrant `~/.config/jarvis/memory`)            |
| Model names, keys                    | `config.py`, `~/.config/jarvis/.env`, `defaults/env.example`    |

Debug first, then edit: read the app's terminal log (T3 Code keeps it in `~/.t3/userdata/logs/terminals/*.log`).
`[User Transcript]` shows what Whisper heard; the lines after it show whether the sentence was passed on or ignored.

## Rules

- **Speech check changes need the benchmark.** Any edit to `INTENT_QUESTION`, `FILTER_MODEL` or `LAYA_MODEL` runs
  `scripts/bench_intent.py` before and after and quotes both results. It exits 1 below 97%; going below needs
  Sang's OK. New misses found in real use become new cases in that script.
- **Routing changes need the routing benchmark.** An edit to an agent `role`, `agent_roster` or the system
  prompt runs `scripts/bench_routing.py` before and after; it exits 1 below 95%.
- **One gateway, two kinds of credential.** Every route but `/health` and `GET /client.py` needs either the shared
  token (`brain_token`, for the CLI and scripts) or a **paired device's secret**. `jarvis pair` mints a single-use
  6-digit code (5 minutes, one code at a time, five wrong guesses in a minute and the code is dropped); the device
  trades it for its own secret via `POST /v1/pair` and keeps it at `~/.config/jarvis-remote/credentials.json`
  (0600, sent as `Authorization: Bearer`, never in a URL). `JARVIS_HOME/devices.json` (0600, one `DeviceStore` shared
  by the server and the relay) holds a SHA-256 per device, never the secret; `GET /v1/devices` and
  `POST /v1/devices/{id}/revoke` are the owner's alone, so a leaked laptop is cut off without disturbing anyone else
  or rotating the shared token (`jarvis_core/devices.py`, `server.py`, `remote_audio.py`).
- **Thin layer, one orchestrator.** Jarvis never does an agent's job: no email, calendar or coding logic here.
  The voice routes every task that needs doing to `brain`; the brain decides which runner (one-shot `pi`, a T3
  thread, a minimal Claude/OpenCode turn, or the board). A new kind of work is a new runner in
  `apps/brain/src/brain/runners/`, not new code in the voice.
- **The checkout is the runtime; one gateway is the surface.** `src/jarvis/supervisor.py` runs the brain and the
  voice in one process on this checkout (no wheel, no `uv tool`). `jarvis start` syncs, builds both compiled
  artifacts (cockpit, mac app), kicks launchd and opens the cockpit; it is the only service command.
  Everything network-shaped is one aiohttp app on `BRAIN_HOST:BRAIN_PORT` (default `0.0.0.0:8643`), built by
  `BrainServer`: the brain's API, the cockpit's telemetry, `/health`, `/v1/status`, `/v1/board`, `/v1/tasks`,
  `/v1/mode`, `/v1/restart`, and the thin client's `/ws` + `/client.py`. Every route but `/health` needs the shared
  token (`Authorization: Bearer`, `?token=`, or `X-Jarvis-Token`). A client on another machine needs no `jarvis`
  install: `jarvis health --url http://<mac-mini>:8643`. Both share one state home, `JARVIS_HOME` (this checkout
  unless set, and gitignored): `.env`, `agents.json`, `SOUL.md`, `SYSTEM.md`, the voiceprint, models, memory, the
  relay token (`remote_token`), the brain's state in `brain/` (board, worktrees, sessions, traces, skills, notes,
  `state.db`), and the token the three apps share (`brain_token`, made on first use; `BRAIN_API_KEY` in `.env`
  wins over it). The brain's `allowed_repo_set` always includes `jarvis` and `brain`, both of which mean this
  checkout. The shipped `agents.json` has one agent, `brain`; the voice never talks to a coding client directly.
- **The brain decides its own approvals.** Its planner defers to the owner for anything protected (`outbox`
  `approval_request`, read out by the voice and answered with `answer_agent`).
- **Every coding client shares one session contract; T3 Code is the first-class one.** T3 Code, Claude Code,
  OpenCode and Pi extend `CodingSessionAdapter` (`sessions.py`: `list_sessions`, `pick_session`, `new_session`,
  `session_id`); T3's sessions are its threads. A new client implements those, with no case of its own in
  `main.py`, `agent_tools.py` or `prompts.py`. What only T3 does (its projects, `jarvis connect` with no agent,
  continuing its newest thread on the first task) is reached through `orchestrator.t3` when T3 is enabled,
  never by checking for a method. A switch never happens mid-turn (`busy`). The shipped roster's one agent,
  `brain`, is a coding-session adapter too: its sessions are T3 threads, listed and picked through
  `GET /v1/sessions` and `POST /v1/connect`.
  By voice, `send_agent_task` takes `session` (`"new"`, or words matched against titles by `match_sessions`) and
  `project` (a listed folder: its newest session that is not running, or a new one) (`agent_tools.py`
  `switch_session`); a connected session ignores both. Results name the session the work went to.
  `list_sessions` reads titles out on request (`owner_only` for a meeting guest); the system prompt lists none.
- **The brain picks the runner.** `BrainOrchestrator.route` ([apps/brain/src/brain/orchestrator.py])
  turns one request into one runner call: `EphemeralPiRunner` for a small self-contained question (a `0700`
  temp dir, destroyed in `finally`), `T3Runner` for project work in a listed folder (one connection, many
  watched threads, `t3 serve` auto-started on loopback), the minimal `ClaudeRunner`/`OpenCodeRunner` when the
  owner names them, or the board. Mode switches, `jarvis connect` sessions and board commands bypass the router.
- **Claude Code uses the user's own settings** (`~/.claude/settings.json`, `defaultMode` and allow rules), as in
  their terminal or T3 Code. Jarvis sets no permission mode; only prompts those settings raise reach Jarvis.
- **T3 Code threads use the user's T3 defaults** (`defaultModelSelection`, `defaultRuntimeMode` in
  `~/.t3/userdata/settings.json`, or the project's model). Jarvis picks no model or mode of its own.
- **Projects come from a folder list, not an agent.** Git repos one or two levels under `projects_dir`
  (`SOUL.md`, default `~/Projects`) go into the prompt, newest first (`.git/index` time, `_last_touched`);
  only a listed folder is opened. With no thread picked, `T3Runner` continues the repo's bound thread from
  `state.db` `threads`, else the newest idle thread in its folder; a running one may be the user's own, typed
  in T3, so it is never steered. With no idle thread, or after `session: "new"`, the next task starts a thread
  with "Sync with the master branch first. Then: <task>" (`_launch`, so T3 titles it); `new_session` sends
  nothing on its own. Questions about the list itself ("how many projects in OpsCom?") are answered from it,
  with no runner.
  A thread picked with `jarvis connect` (`current_thread`, `recent_threads`, `POST /v1/connect`) gets no message
  at all: it is the user's work in progress. For the rest of that run every task goes to it, even one naming a
  project; Jarvis starts no other thread (`pinned`), and the system prompt names it (`connected_thread`).
  T3 gives project scripts only folders (`T3CODE_PROJECT_ROOT`,
  `T3CODE_WORKTREE_PATH`), no thread id: a worktree names its thread, a project root takes the thread viewed last.
- **A connected session is the default agent.** `jarvis connect claude|opencode|pi` lists that client's sessions
  (every folder, newest first) and `pick_session(connected=True)`; `default_agent_id` returns it for the run.
  Claude Code runs one SDK process per session, started with `resume=<id>`, and resumes the same id after a crash.
- **Pi is reached only through an open window.** `jarvis connect pi` installs `defaults/pi/jarvis.ts` into
  `~/.pi/agent/extensions/`; it serves a socket per Pi process in `~/.config/jarvis/pi` (mode 0700: a connection can
  make Pi run commands). Jarvis sends prompts and stops there and never answers a Pi dialog; it says one is waiting.
- **All user state lives in `JARVIS_HOME`** (`packages/jarvis-core/src/jarvis_core/home.py`): `.env`, `agents.json`,
  `SOUL.md`, `SYSTEM.md`, the voiceprint, models, memory, the relay token (`remote_token`, when `REMOTE_TOKEN` pins one),
  the brain's own state in `brain/` (board, worktrees, sessions, traces, skills, progress notes), and the token the
  three apps share (`brain_token`, made on first use; `BRAIN_API_KEY` in `.env` wins over it). The installed `jarvis` uses
  `~/.config/jarvis`; `uv run jarvis` in a checkout uses that checkout's
  folder (`checkout_root`, found from the running code, never the current folder; gitignored) and stops without its
  `.env`. The T3 setup script copies that state into new worktrees. The one-mic lock stays in `~/.config/jarvis`
  for both. A relative `WATCH_CONFIG_PATH` counts from `JARVIS_HOME`. `jarvis init` copies the defaults from
  `src/jarvis/defaults/` (shipped in the package); a change to the shipped agents only reaches users who copy it.
- **"Stop" stops the work, "shut up" mutes Jarvis.** "Stop it.", "stop that", "cancel that" reach the
  conversation model, which calls `stop_agent` (`nothing_running` when idle); only "shut up" / "be quiet" (and
  "stop talking", "hush"; "im đi", "im lặng", "đừng nói nữa") mute, and agents keep working while muted.
  `bench_intent.py` scores mute exactly.
- **Jarvis sees the screen only when pointed at it.** `look_at_screen` answers a question about the front window
  with one stateless `EXECUTIVE_MODEL` call; `send_agent_task` `include_screen` adds the PNG's path to the task. Shots
  live in `JARVIS_HOME/screen` (0700/0600, newest 5; gitignored in a checkout); images never enter the context or memory. A
  meeting guest gets `owner_only`. Needs macOS Screen Recording for the app running Jarvis.
- **Approvals and questions never go through an LLM.** Agent questions and approval requests are spoken as they
  are; only finished turns (`AgentTopic.is_report`) go to the conversation model, as a system message that calls
  no tool. This keeps the model from approving anything on its own.
- **Timers and outbox delivery are code, not prompt.** A schedule is a five-field cron read in its own zone, and
  a due row is advanced atomically before any worker runs (`fire_due_timers`); a missed slot is skipped, never
  replayed in a burst. In `ASSIST` mode a queued outbox item is published to the voice as it lands; in
  `AUTONOMOUS` mode it stays quiet until the owner switches back or asks for the brief. The voice acknowledges it
  (`POST /v1/outbox/{id}/spoken`) before it enters `memory/reported.md`, so the brain never re-reports it.
- **Jarvis speaks as one assistant.** Replies and retold turns say "I pushed it", "I'm searching", never which
  agent does the work, unless the user asks. A retold turn is the outcome against the user's request in about ten
  words; a turn that asks the user to decide (a plan, options) gets one phrase per heading, at most four, then its
  question (`AGENT_REPORT_PROMPT`); the full text stays in the LLM context for follow-up questions.
- **Jarvis talks in the user's language, agents get English.** `LANGUAGE` (`en` or `vi`, `config.py`) sets the
  language Whisper listens for (a per-sentence guess misreads short Vietnamese) and, with `WHISPER_MODEL` empty, the
  model (`WHISPER_MODELS`: turbo for `en`; for `vi`, `sangdth/phowhisper-large-vien-cs-mlx-16bit`: VinAI's
  PhoWhisper-large plus the vi-en code-switching LoRA `rinhoooo/phowhisper-large-vien-cs-asr`, merged and converted
  to MLX by us). Language-specific prompt rules live in `languages.json` and are added to the system and greeting
  prompts dynamically for the active `LANGUAGE` only. Replies, retold turns and the greeting follow the user's
  language. Every `task` and `answer` is written in English (`agent_tools.py`), so `is_approval` stays English-only.
  Fixed spoken lines (the `jarvis connect` line) live in `languages.json` per language. Its `waiting` lines are the
  base for the "still waiting" lines: `EXECUTIVE_MODEL` writes 6 new ones per task from them and the user's task
  (never an agent's text, never a progress claim), refilling at 4 used; the base lines are spoken only when that fails.
  Agent questions and approvals stay English: they never go through an LLM.
- **In a meeting Jarvis has one master; everyone else is a guest.** `jarvis meeting` tags each sentence `[User]` or
  `[Room]` (`SpeakerVerifiedWhisperSTT`) and buffers room talk until someone says "Jarvis" (`JarvisIntentGate`).
  A room turn sets `JarvisState.room_turn`; `answer_agent`, `stop_agent` and a `replace` then return `owner_only`,
  and with `MEETING_GUESTS=ask` (default) a `send_agent_task` returns `needs_owner_ok` and runs nothing until the
  master's yes resends it (`agent_tools.py`, in code, not in the prompt; `allow` runs guest tasks). With the master
  check off nobody is `[Room]`: everyone counts as the master. Memory neither saves meeting turns nor searches for
  the room. **Guests may add comments, and only comments:** `add_comment` / `read_comments` exist only in meeting
  mode, write `JARVIS_HOME/meeting/<date>.md` with the speaker label from `JarvisState.current_speaker`, and touch
  no agent, no approval and no memory (`meeting_notes.py`). **The mode itself is switchable by voice:** "meeting mode
  on" / "meeting mode off" are fast local commands (`state.classify_fast_command`, no model call) handled before the
  mute check; only the master may switch it (`master_check` and `is_owner`), and the rules are handed to the model as
  a system note (`JarvisState.notify`) so no restart is needed. `JarvisState.meeting` is the one source of truth —
  the gate, the STT and the comment tools read it live; `jarvis meeting` at startup is just its initial value.
- **Watcher strings start with the verb** (`asks: ...`, `needs approval: ...`); `JarvisState` adds the agent name.
- **Agent ids are exact** (`brain` in the shipped roster; `claude`, `opencode`, `t3-code` exist only when an
  `agents.json` enables them), matched as written. A bad `DEFAULT_CODING_AGENT` stops Jarvis at startup.
- **Model settings, one job each:** `EXECUTIVE_MODEL` talks, retells turns and writes the waiting lines, thinking always on;
  `LAYA_MODEL` (Laya's `jarvis-intent`) decides whether a sentence is meant for Jarvis, with `FILTER_MODEL` (an
  OpenRouter decision model on `/api/alpha/decisions`) as its fallback; `EXTRACTION_MODEL` picks memory facts, a
  fast model with thinking always off; `CLEANUP_MODEL` (empty = `EXECUTIVE_MODEL`, thinking off) tidies each passed
  sentence after the speech check and before the speaker labels (`cleanup.py`).
- **The speech check asks Laya first.** `LAYA_MODEL` (`jarvis-intent`, fine-tuned for Jarvis, served by
  HappyVoxel's GPU box at `LAYA_BASE_URL` with a console `LAYA_API_KEY`) answers; `FILTER_MODEL` is asked at the
  same time and used when Laya fails or passes `LAYA_TIMEOUT_SECS`. Every decision, both answers and the
  corrections the user's next move implies (an undone mute, "not you", an ignored sentence said again) go to
  `feedback/intent.jsonl` in `JARVIS_HOME`, for retraining in the laya repo (`data/distill.py feedback`).
- **Voice is ElevenLabs `eleven_v4_turbo` over Text-to-Dialogue** (the plain TTS socket rejects it). Emotion
  tags come from Pipecat's expressive conversation mode; Jarvis writes no tag rules of its own.
- **Memory is local Mem0** (open source, Apache 2.0): Qdrant on disk in `~/.config/jarvis/memory`, `bge-m3` embeddings
  on the Ollama app, fact extraction on `EXTRACTION_MODEL`. Telemetry stays off (`MEM0_TELEMETRY=False`). It saves only the newest exchange. Without Ollama, Jarvis runs with memory off and logs why.
- **The master check is on by default.** `MASTER_CHECK=true` (the default; with no `owner_voice.npy` yet, the first
  person who addresses Jarvis claims ownership naturally across their opening sentences, and `jarvis enroll` resets it
  through a short chat): `SpeakerVerifiedWhisperSTT` scores each whole Whisper sentence against
  `JARVIS_HOME/owner_voice.npy` (sherpa-onnx, 3D-Speaker ERes2Net en) and drops other
  voices; mute and unmute phrases pass from any voice; `jarvis init` writes `MASTER_CHECK=false` when the user
  declines voice locking. User turns come only
  from `JarvisIntentGate` (`ExternalUserTurnStrategies`); raw room sound never starts, ends or interrupts a turn.
  `MASTER_CHECK=false` lets any voice talk to Jarvis and skips loading the speaker model.
- **`~/.config/jarvis/SOUL.md` is the user's own setup** (`jarvis init` in `init.py`, read by `soul.py`): front
  matter holds settings (`projects_dir`), the body is personality, added to the system prompt as tone only. The name
  is always Jarvis.
- **`SYSTEM.md` in `JARVIS_HOME` overrides shipped prompts, one `## Name` section each** (`system_prompts.py`,
  `KNOWN`: only `Cleanup` today). Optional: `init` never writes it, and a missing section keeps the prompt in
  `prompts.py`, so our fixes still reach the user. Read once at startup; the log names what it overrides.
- Mock HTTP and the Claude SDK in unit tests. Live checks against real agents go in a throwaway script, not in `tests/`.

## Later, not now

- Pi as a routable agent (in `agents.json`, not only `jarvis connect pi`), and Codex: a new `<name>_adapter.py` shaped like `opencode_adapter.py` / `hermes_adapter.py`,
  registered in `orchestrator.py` `ADAPTERS`; or run them as a T3 Code provider and reach them through `t3-code`.
- Ideas from Hex (replaying saved clips through the pipeline, window context, `hex mcp`):
  `docs/research/hex-adoption-plan.md`.
