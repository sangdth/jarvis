# Jarvis

An always-on voice harness (Pipecat) that talks to the user and hands work to agents: coding to T3 Code
(or Claude Code or OpenCode when named), everything else to Hermes Agent. No wake word. Jarvis stays thin: it routes and
relays; the work, tools and memory live in each agent's own harness.
Architecture: `docs/architecture.md`. Setup and voice commands: `README.md`.

## Commands

- Install: `uv tool install --editable .` puts `jarvis` on the PATH. Commands: `jarvis` (listen), `jarvis connect`
  (continue a T3 thread; as a T3 project script, the thread it runs in), `jarvis init` (set up, then enroll),
  `jarvis enroll` (voiceprint again). `uv run jarvis …` works the same in the repo.
- Needs: OpenCode `opencode serve --port 4096` with the same password; Hermes its gateway service
  (`hermes gateway status`, `API_SERVER_ENABLED=true`); T3 Code the T3 app running.
- Test: `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`
- Speech benchmark: `uv run python scripts/bench_intent.py` (paid OpenRouter calls, about 110 at 3 rounds)
- Routing benchmark: `uv run python scripts/bench_routing.py` (about 120 paid calls)
- Landing page: `site/index.html`, one static file; `.github/workflows/pages.yml` publishes it to
  <https://sangdth.github.io/jarvis/> on every push to master that touches `site/`.

## Where to fix "Jarvis"

Start from what the user heard or didn't. The obvious file is often wrong: "Jarvis ignores me" is usually the
speech check dropping the sentence, not the conversation model.

| Symptom                              | Where                                                         |
| :----------------------------------- | :------------------------------------------------------------ |
| Doesn't react to something said      | `executive_filter.py` `INTENT_QUESTION`                       |
| Mute / unmute phrase missed          | `state.py` `_MUTE_PATTERNS`, `_UNMUTE_PATTERNS`               |
| Reacts, but the reply is wrong       | `main.py` `JARVIS_EXECUTIVE_SYSTEM_PROMPT`                    |
| Says something wrong about an agent  | `agent_watchers.py` `_surface` call sites                     |
| Retold agent turn is long or odd     | `main.py` `AGENT_REPORT_PROMPT`, `state.py` `_retell`         |
| Wrong agent gets a task              | `agents.json` `role`, `main.py` `agent_roster`                |
| Answer or stop hits the wrong agent  | `agent_watchers.py` `find_waiting`, `find_busy`               |
| A correction runs as a second task   | `main.py` `send_agent_task` `replace`, each `interrupt`       |
| "Still waiting" timing or wording    | `agent_watchers.py` `remind_while_busy`, `WAITING_LINES`      |
| Hermes runs, approvals, stop         | `agent_watchers.py` `HermesAdapter`                           |
| T3 Code threads, approvals, stop     | `t3_adapter.py` `T3Adapter` (WebSocket, protocol 2)           |
| Beeps, muted queue, unmute digest    | `state.py` `JarvisState`                                      |
| Mic, Whisper, interruptions          | `main.py` pipeline, `voice_gates.py`                          |
| Hears itself through the speakers    | `voice_gates.py` `EchoFilter`, `EchoReference`                |
| Drops my voice / lets others in      | `[Speaker] score=` log lines, `SPEAKER_THRESHOLD`             |
| Cut off, or splits one sentence      | `main.py` `VADParams` `stop_secs`, `JarvisIntentGate`         |
| Voice sound, laughs, emotion         | `main.py` `ElevenLabsDialogueTTSService`, `expression_policy` |
| Forgets, remembers wrong, slow reply | `memory.py` (Mem0, Qdrant `~/.config/jarvis/memory`)          |
| Model names, keys                    | `config.py`, `~/.config/jarvis/.env`, `.env.example`          |

Debug first, then edit: read the app's terminal log (T3 Code keeps it in `~/.t3/userdata/logs/terminals/*.log`).
`[User Transcript]` shows what Whisper heard; the lines after it show whether the sentence was passed on or ignored.

## Rules

- **Speech check changes need the benchmark.** Any edit to `INTENT_QUESTION` or `FILTER_MODEL` runs
  `scripts/bench_intent.py` before and after and quotes both results. It exits 1 below 97%; going below needs
  Sang's OK. New misses found in real use become new cases in that script.
- **Routing changes need the routing benchmark.** An edit to an agent `role`, `agent_roster` or the system
  prompt runs `scripts/bench_routing.py` before and after; it exits 1 below 95%.
- **Thin layer.** Jarvis never does an agent's job: no email, calendar or coding logic here. A new kind of
  work is a new `role` (or a new agent in `agents.json`), not new code in Jarvis.
- **Hermes decides its own approvals.** Its guardian model (`approvals` in `~/.hermes/config.yaml`) lets safe
  commands run; only flagged ones reach Jarvis as `approval.request`. A question Hermes asks ends its turn, so
  the reply goes back with `send_agent_task` into the same Hermes session.
- **Claude Code uses the user's own settings** (`~/.claude/settings.json`, `defaultMode` and allow rules), as in
  their terminal or T3 Code. Jarvis sets no permission mode; only prompts those settings raise reach Jarvis.
- **T3 Code threads use the user's T3 defaults** (`defaultModelSelection`, `defaultRuntimeMode` in
  `~/.t3/userdata/settings.json`, or the project's model). Jarvis picks no model or mode of its own.
- **Projects come from a folder list, not an agent.** Git repos one or two levels under `projects_dir` (`SOUL.md`, default
  `~/Projects`) go into the prompt; `T3Adapter.open_project` only opens a listed folder and starts its thread with only "Sync with the
  master branch."; the model's task is not sent. Later requests go to that thread.
  A thread picked with `jarvis connect` (`current_thread`, `recent_threads`, `T3Adapter.attach`) gets no message
  at all: it is the user's work in progress. T3 gives project scripts only folders (`T3CODE_PROJECT_ROOT`,
  `T3CODE_WORKTREE_PATH`), no thread id: a worktree names its thread, a project root takes the thread viewed last.
- **All user state lives in `JARVIS_HOME`** (`config.py`, `~/.config/jarvis`): `.env`, `agents.json`, `SOUL.md`,
  the voiceprint, models, memory. Never the current folder: `jarvis` runs from any project, and a project's own
  `.env` must not leak in. A relative `WATCH_CONFIG_PATH` counts from there.
- **Approvals and questions never go through an LLM.** Agent questions and approval requests are spoken as they
  are; only finished turns (`AgentTopic.is_report`) go to the conversation model, as a system message that calls
  no tool. This keeps the model from approving anything on its own.
- **Watcher strings start with the verb** (`asks: ...`, `needs approval: ...`); `JarvisState` adds the agent name.
- **Agent ids are exact** (`claude`, `opencode`, `t3-code`, `hermes`), matched as written in `agents.json`. A bad
  `DEFAULT_CODING_AGENT` stops Jarvis at startup.
- **Two model settings, one job each:** `EXECUTIVE_MODEL` talks and retells turns; `FILTER_MODEL` (a decision
  model on `/api/alpha/decisions`) decides whether a sentence is meant for Jarvis.
- **Voice is ElevenLabs `eleven_v4_turbo` over Text-to-Dialogue** (the plain TTS socket rejects it). Emotion
  tags come from Pipecat's expressive conversation mode; Jarvis writes no tag rules of its own.
- **Memory is local Mem0** (open source, Apache 2.0): Qdrant on disk in `~/.config/jarvis/memory`, `bge-m3` embeddings
  on the Ollama app, fact extraction on `EXECUTIVE_MODEL`. Telemetry stays off (`MEM0_TELEMETRY=False`). It saves
  only the newest exchange. Without Ollama, Jarvis runs with memory off and logs why.
- **Jarvis acts only on the owner's voice.** `SpeakerVerifiedWhisperSTT` scores each whole Whisper sentence
  against `~/.config/jarvis/owner_voice.npy` (sherpa-onnx, 3D-Speaker ERes2Net en); mute and unmute phrases pass from any
  voice. No voiceprint stops Jarvis at startup (`jarvis enroll`). User turns come only from
  `JarvisIntentGate` (`ExternalUserTurnStrategies`); raw room sound never starts, ends or interrupts a turn.
- **`~/.config/jarvis/SOUL.md` is the user's own setup** (`jarvis init`, `soul.py`): front matter holds settings
  (`projects_dir`), the body is personality, added to the system prompt as tone only. The name is always Jarvis.
- Mock HTTP and the Claude SDK in unit tests. Live checks against real agents go in a throwaway script, not in `tests/`.

## Later, not now

- More agents (Pi, Codex): add an adapter with the same shape as `OpenCodeSSEAdapter` / `HermesAdapter`, or run
  them as a T3 Code provider and reach them through `t3-code`.
- Laya as a local speech check: parked; it lost to the OpenRouter decision models.
