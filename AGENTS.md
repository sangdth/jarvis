# Jarvis

An always-on voice harness (Pipecat) that talks to the user and hands work to agents: coding to T3 Code
(or Claude Code or OpenCode when named), everything else to Hermes Agent. No wake word. Jarvis stays thin: it routes and
relays; the work, tools and memory live in each agent's own harness.
Architecture: `docs/architecture.md`. Choices with known limits: `docs/DECISIONS.md`. Setup and voice commands:
`README.md`.

## Commands

- Production: `curl -fsSL https://sangdth.github.io/jarvis/install.sh | sh` (`site/install.sh`: uv, then
  `uv tool install --python 3.12` from master, then `jarvis init`); `jarvis update` upgrades it,
  `jarvis --version` names the commit. Dev: `uv run jarvis …` in a checkout runs that checkout (`cli.py`).
  Commands: `jarvis` (listen), `jarvis connect` (continue a T3 thread; as a T3 project script, the thread it runs
  in), `jarvis connect claude|opencode|pi` (continue a session of that client; for Pi, an open window), `jarvis meeting` (sit in a call; `--meeting` after `connect`), `jarvis init` (set up, then enroll), `jarvis enroll` (voiceprint again), `jarvis uninstall`
  (deletes `~/.config/jarvis`, then `uv tool uninstall`; a dev checkout keeps its tool and state). One listens at a time
  (`hold_lock`, `~/.config/jarvis/jarvis.lock`).
- Needs: OpenCode 2's own service (found in `~/.local/state/opencode/service.json`), or `opencode serve` pinned with
  `OPENCODE_SERVER_PASSWORD`; Hermes its gateway service
  (`hermes gateway status`, `API_SERVER_ENABLED=true`); T3 Code the T3 app running.
- Test: `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`
- Lint and types: `uv run ruff check src tests scripts` and `uv run mypy` (config in `pyproject.toml`)
- Speech benchmark: `uv run python scripts/bench_intent.py` (paid OpenRouter calls, about 150 at 3 rounds)
- Routing benchmark: `uv run python scripts/bench_routing.py` (about 240 paid calls; `bench_routing.py 1` runs one round, 80)
- Landing page: `site/index.html`, one static file; `.github/workflows/pages.yml` publishes it to
  <https://sangdth.github.io/jarvis/> on every push to master that touches `site/`.

## Where to fix "Jarvis"

Start from what the user heard or didn't. The obvious file is often wrong: "Jarvis ignores me" is usually the
speech check dropping the sentence, not the conversation model.

| Symptom                              | Where                                                           |
| :----------------------------------- | :-------------------------------------------------------------- |
| Doesn't react to something said      | `executive_filter.py` `INTENT_QUESTION`, Laya `jarvis-intent`   |
| Mute / unmute phrase missed          | `state.py` `_MUTE_PATTERNS`, `_UNMUTE_PATTERNS`                 |
| Which model answered, corrections    | `~/.config/jarvis/feedback/intent.jsonl`, `feedback.py`         |
| Heard right, reached the model wrong | `[Cleanup]` log, `prompts.py` `CLEANUP_PROMPT`, `cleanup.py`  |
| Reacts, but the reply is wrong       | `prompts.py` `JARVIS_EXECUTIVE_SYSTEM_PROMPT`, `languages.json` |
| Says something wrong about an agent  | `*_adapter.py` `_surface` call sites                            |
| Retold agent turn is long or odd     | `prompts.py` `AGENT_REPORT_PROMPT`, `state.py` `_retell`        |
| Wrong agent gets a task              | `agents.json` `role`, `prompts.py` `agent_roster`               |
| Answer or stop hits the wrong agent  | `orchestrator.py` `find_waiting`, `find_busy`                   |
| A correction runs as a second task   | `agent_tools.py` `send` (`replace`), each `interrupt`           |
| "Still waiting" timing or wording    | `remind_while_busy`, `waiting_lines.py`, `WAITING_PROMPT`       |
| Hermes runs, approvals, stop         | `hermes_adapter.py` `HermesAdapter`                             |
| T3 Code threads, approvals, stop     | `t3_adapter.py` `T3Adapter` (WebSocket, protocol 2)             |
| `jarvis connect <agent>`, sessions   | `sessions.py` `CodingSessionAdapter`, `main.py` `pick_session`  |
| Pi windows                           | `pi_adapter.py` `PiAdapter`, `defaults/pi/jarvis.ts`            |
| Beeps, muted queue, unmute digest    | `state.py` `JarvisState`                                        |
| Mic, Whisper, interruptions          | `main.py` pipeline, `voice_gates.py`                            |
| Hears itself through the speakers    | `mac_audio.py` `MacVoiceAudio` (macOS voice processing)         |
| Hears nothing, "Mic is silent" log   | Output is Multi-Output; `mac_audio.py` `SILENT_MIC_WARNING`     |
| Meeting: call or Jarvis can't hear   | `mac_audio.py` `MeetingAudioBridge`, `TAP_FAILED` log           |
| Drops my voice / lets others in      | `[Speaker] score=` log lines, `SPEAKER_THRESHOLD`               |
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
- **Thin layer.** Jarvis never does an agent's job: no email, calendar or coding logic here. A new kind of
  work is a new `role` (or a new agent in `agents.json`), not new code in Jarvis.
- **Hermes decides its own approvals.** Its guardian model (`approvals` in `~/.hermes/config.yaml`) lets safe
  commands run; only flagged ones reach Jarvis as `approval.request`. A question Hermes asks ends its turn, so
  the reply goes back with `send_agent_task` into the same Hermes session.
- **T3 Code is the first-class client; Claude Code, OpenCode and Pi share one session contract.** T3 keeps its own
  projects and threads (`T3Adapter`), reached only through `orchestrator.t3`, never by checking for a method. The
  others extend `CodingSessionAdapter` (`sessions.py`: `list_sessions`, `pick_session`, `new_session`); a new
  client of that kind implements those three, with no case of its own in `main.py`, `agent_tools.py` or
  `prompts.py`. A switch never happens mid-turn (`busy`). Hermes is not a coding client and stays on the base.
  By voice, `send_agent_task` takes `session` (`"new"`, or words matched against titles by `match_sessions`) and
  `project` (a listed folder: its newest session, or a new one) for these clients (`agent_tools.py`
  `switch_session`); a connected session ignores both. The system prompt lists no sessions.
- **Claude Code uses the user's own settings** (`~/.claude/settings.json`, `defaultMode` and allow rules), as in
  their terminal or T3 Code. Jarvis sets no permission mode; only prompts those settings raise reach Jarvis.
- **T3 Code threads use the user's T3 defaults** (`defaultModelSelection`, `defaultRuntimeMode` in
  `~/.t3/userdata/settings.json`, or the project's model). Jarvis picks no model or mode of its own.
- **Projects come from a folder list, not an agent.** Git repos one or two levels under `projects_dir`
  (`SOUL.md`, default `~/Projects`) go into the prompt, newest first (`.git/index` time, `_last_touched`);
  `T3Adapter.open_project` only opens a listed folder and starts its thread with only "Sync with the
  master branch."; the model's task is not sent. Later requests go to that thread. Questions about the list
  itself ("how many projects in OpsCom?") are answered from it, with no agent.
  A thread picked with `jarvis connect` (`current_thread`, `recent_threads`, `T3Adapter.attach`) gets no message
  at all: it is the user's work in progress. For the rest of that run every task goes to it, even one naming a
  project; Jarvis starts no other thread (`connected`), and the system prompt names it (`connected_thread`).
  T3 gives project scripts only folders (`T3CODE_PROJECT_ROOT`,
  `T3CODE_WORKTREE_PATH`), no thread id: a worktree names its thread, a project root takes the thread viewed last.
- **A connected session is the default agent.** `jarvis connect claude|opencode|pi` lists that client's sessions
  (every folder, newest first) and `pick_session(connected=True)`; `default_agent_id` returns it for the run.
  Claude Code runs one SDK process per session, started with `resume=<id>`, and resumes the same id after a crash.
- **Pi is reached only through an open window.** `jarvis connect pi` installs `defaults/pi/jarvis.ts` into
  `~/.pi/agent/extensions/`; it serves a socket per Pi process in `~/.config/jarvis/pi` (mode 0700: a connection can
  make Pi run commands). Jarvis sends prompts and stops there and never answers a Pi dialog; it says one is waiting.
- **All user state lives in `JARVIS_HOME`** (`config.py`): `.env`, `agents.json`, `SOUL.md`, `SYSTEM.md`, the voiceprint,
  models, memory. The installed `jarvis` uses `~/.config/jarvis`; `uv run jarvis` in a checkout uses that checkout's
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
  the room.
- **Watcher strings start with the verb** (`asks: ...`, `needs approval: ...`); `JarvisState` adds the agent name.
- **Agent ids are exact** (`claude`, `opencode`, `t3-code`, `hermes`), matched as written in `agents.json`. A bad
  `DEFAULT_CODING_AGENT` stops Jarvis at startup.
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
- **The master check is opt-in.** `MASTER_CHECK=false` (default): any voice talks to Jarvis and the speaker model
  is not loaded. `MASTER_CHECK=true` (`jarvis enroll` sets it): `SpeakerVerifiedWhisperSTT` scores each whole
  Whisper sentence against `~/.config/jarvis/owner_voice.npy` (sherpa-onnx, 3D-Speaker ERes2Net en) and drops other
  voices; mute and unmute phrases pass from any voice; no voiceprint stops Jarvis at startup. User turns come only
  from `JarvisIntentGate` (`ExternalUserTurnStrategies`); raw room sound never starts, ends or interrupts a turn.
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
