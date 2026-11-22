# Jarvis — Architecture

## 0. Three apps, one repository

| App | Package / binary | Job | Where |
| :--- | :--- | :--- | :--- |
| Voice layer | `jarvis-voice` (Python) | hears, checks the sentence, talks, drives agents | `apps/voice/` |
| Brain | `brain` (Python) | board of goals/problems, `pi` workers in worktrees, HTTP/SSE | `apps/brain/` |
| Cockpit | `jarvis-top` (Rust) | fullscreen board, attempts, spend and live telemetry | `apps/tui/` |

`packages/jarvis-core` holds what all three share: `REPO_ROOT` and `JARVIS_HOME`, the `.env` settings base, the
tokens and `SOUL.md`. `src/jarvis` is the one command (`jarvis`) that starts, stops and talks to the stack.

The checkout is the runtime. `jarvis` runs the brain and the voice in one process on this checkout (`uv sync` is
the only build step, and only when dependencies changed), so `jarvis restart` is the whole update loop. One aiohttp
app on `BRAIN_HOST:BRAIN_PORT` (default `0.0.0.0:8643`) is the only network surface: the brain's API, the cockpit's
telemetry, `/health`, `/v1/status`, `/v1/board`, `/v1/tasks`, `/v1/mode`, `/v1/restart`, and the thin client's
`/ws` + `/client.py`. Every route but `/health` needs the shared token. One machine, one board, one SQLite writer,
and one client at a time on the mic.

Each language keeps its native workspace (`uv`, `cargo`; `pnpm`/`go.work` when a client or service in another
language arrives), so a new app is a folder and a workspace line. Cross-language contracts live in `schemas/`.

## 1. Core Requirements

1. **Always-On (No Wake Word), Respond Only When Needed:** Continuously listen without requiring *"Hey Jarvis"*, while distinguishing between self-talk/background conversation and utterances actually addressed to Jarvis.
2. **Multi-Agent Orchestration:** The voice routes every task that needs doing to **the brain** (`brain`), which
   always runs alongside it. The brain decides the runner: coding and project work in a **T3 Code** thread, a small
   question in a one-shot `pi` worker, a named Claude Code or OpenCode turn in that client, and overnight or
   self-upgrade work on its board. Jarvis only routes and relays; the work happens in each runner's own harness.
3. **Plain-Words Reporting:** Agent questions and approval requests are read out as they are. A finished turn is long markdown full of file paths and diffs; Jarvis has its conversation model retell it as one short first-person outcome, about ten words; a plan or choice waiting on the user gets one phrase per heading, then its question (`AGENT_REPORT_PROMPT`).
4. **Dynamic Silence / Notification Mode:**
   - Saying **"Shut up"** or **"Be quiet"** immediately silences Jarvis's voice (`BEEP_ONLY` mode), interrupting any active speech and using short audio chimes ("beep") to notify when new agent decisions arrive.
   - Saying **"You can talk"** restores `VOICE` mode and reads out a concise digest of queued topics.
   - **"Stop" stops the work, "shut up" mutes Jarvis.** "Stop it", "stop that" and "cancel that" reach the
     conversation model, which calls `stop_agent`; agents keep working while Jarvis is muted.

---

## 2. System Architecture Diagram

> [!TIP]
> A visual architecture map is stored in [docs/architecture-map.html](architecture-map.html) (standalone interactive view) and [docs/assets/architecture-map.png](assets/architecture-map.png).

```mermaid
flowchart LR
    subgraph Input ["1. Always-On Ear"]
        Mic["Mic Input"] --> VAD["Silero VAD"]
        VAD --> STT["Local STT (MLX Whisper) + master check (default)"]
    end

    subgraph Brain ["2. Jarvis Executive Filter"]
        STT --> Intent{"Intent Router"}
        Intent -->|"Shut up / Be quiet"| Mute["Set mode = BEEP_ONLY"]
        Intent -->|"You can talk"| Unmute["Set mode = VOICE & Read Digest"]
        Intent -->|"Addressed to Jarvis"| LLM["Executive LLM (Context + Summary)"]
    end

    subgraph Orchestration ["3. BrainClient — one orchestrator"]
        BC["BrainClient (HTTP/SSE)"] --> ROUTE["BrainOrchestrator.route"]
        ROUTE -->|"small question"| EP["Ephemeral `pi` (temp dir, destroyed)"]
        ROUTE -->|"project work"| T3["T3Runner (long-lived threads)"]
        ROUTE -->|"named client"| MIN["ClaudeRunner / OpenCodeRunner (minimal)"]
        ROUTE -->|"overnight / self-upgrade"| BOARD["Board + `pi` Builder"]
        EP --> Buffer["Questions, approvals, finished turns"]
        T3 --> Buffer
        MIN --> Buffer
        BOARD --> Buffer
        Buffer -->|"spoken verbatim or retold"| Notify2{"Check Mode"}
    end

    Mute -.-> Notify2
    Unmute -.-> Notify2
    Notify2 -->|"mode == BEEP_ONLY"| Beep2["Play Beep + Queue Topic"]
    Notify2 -->|"mode == VOICE"| LLM2["Executive LLM retells finished turns"]
    LLM2 --> TTS2["TTS Output to Speaker"]
    LLM -->|"send_agent_task / answer_agent / stop_agent"| BC

    subgraph BrainApp ["4. The brain (`apps/brain`)"]
        SB["Board (SQLite WAL)"] --> REC["Reconciler"]
        REC -->|"one `pi` worker per repo"| WT["Worktree `brain/<task_id>`"]
        WT -->|"verify_cmd, then push (never merge)"| SB
        SRV["HTTP/SSE/WS :8643"] --- SB
        TUI["`jarvis top` (Rust cockpit)"] -->|"read-only state.db + /v1/telemetry"| SB
    end

    BC -->|"POST /v1/runs, GET /v1/runs/:id/events"| SRV
```

---

## 3. Stack

### A. Real-Time Voice Pipeline Framework
* **[Pipecat](https://github.com/pipecat-ai/pipecat) (Python)**
  * **Why:** Frame-based architecture makes it trivial to insert custom gates (`JarvisIntentGate`) directly into the audio/transcription stream. Supports instant interruption (`InterruptionFrame`), raw PCM beep injection (`OutputAudioRawFrame`), and direct TTS synthesis (`TTSSpeakFrame`).

### B. "No Wake Word"
Without a wake word, two local filtering stages prevent false triggers and API cost blowups:
0. **Echo cancellation:** `MacVoiceTransport` (`mac_audio.py`) replaces Pipecat's PyAudio transport. One `AVAudioEngine` with macOS voice processing on owns both the mic tap and the speaker player, because Apple cancels only the echo of what plays through the same engine. The mic arrives as float32 at 48 kHz and is resampled to 16 kHz; playback is paced to real time with about 80 ms queued, so an interruption cuts Jarvis off. `jarvis enroll` records through the same engine, so the voiceprint matches what Jarvis hears. Research and numbers: `docs/research/echo-cancellation.md`.
   A paired Mac's mic and speaker reach Jarvis on the same HTTP server as everything else: `RelayHub` and
   `RemoteVoiceTransport` (`remote_audio.py`) serve `/ws` and `/client.py`, and the socket carries 16-bit mono PCM
   at 16 kHz both ways (local speech is 24 kHz and resampled for it). The *client* runs the same `AVAudioEngine`
   voice processing (`defaults/remote/client.py`) — the canceller has to sit where the speaker is, or Jarvis hears
   itself and barge-in dies. The heavy half never moves: Whisper, the speech check, the agents and memory all stay
   on the Mac running Jarvis. The routes are **always mounted**; `MacVoiceTransport` holds a `RelayHub` and hands
   the mic and speaker over to a connected client, and takes them back when it goes, so there is no `jarvis remote`
   mode to switch into. With no local audio hardware (a headless Mac mini), the transport starts without one,
   `has_local_audio` is false, and Jarvis waits for a client before greeting. Output is paced exactly like the local
   transport, and an `InterruptionFrame` sends a `flush` so the other Mac drops what it still had queued. The port
   is on the network, so every connection carries the shared token (`brain_token`, or `remote_token` /
   `REMOTE_TOKEN`) or a paired device's secret, and only one client at a time, since they share one ear.
1. **VAD (Voice Activity Detection):** **[Silero VAD](https://github.com/snakers4/silero-vad)** detects human speech boundaries locally in `<1ms`.
2. **Local Always-On STT + Turn Classifier:** **[MLX Whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper)** (`mlx-community/whisper-large-v3-turbo`) transcribes speech locally on the Apple GPU at $0 cost. With `MASTER_CHECK=true` (on by default; `jarvis enroll` records the voiceprint, `MASTER_CHECK=false` turns the check off), in the same step a [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) speaker embedding (3D-Speaker ERes2Net, about 100 ms, run alongside Whisper) compares the whole sentence with the enrolled voiceprint (`JARVIS_HOME/owner_voice.npy`); below `SPEAKER_THRESHOLD` the sentence is dropped, unless it is a mute or unmute phrase, or `jarvis meeting` tags it `[Room]`. Turns come only from sentences that pass both checks: the user aggregator runs on Pipecat's `ExternalUserTurnStrategies`, and `JarvisIntentGate` wraps each passed sentence in proposed start/stop frames, so background voices never interrupt Jarvis or hold a turn open. Voice detection still cuts Whisper's sentences: with `SMART_TURN=true` (default) a 0.2 s pause asks Pipecat's Smart Turn v3 (local ONNX, about 50 ms) whether the voice sounds finished; if not, `SpeakerVerifiedWhisperSTT` keeps the sentence open until the user goes on or 1.5 s of silence pass, so Whisper and the voice check get it whole; a sentence held past 30 s (`MAX_HELD_SECS`, a TV that never sounds done) goes on as it is. With `SMART_TURN=false` every 0.6 s pause ends a sentence, and one Whisper ends with "..." or a dash waits up to 2 s after the user's next silence and joins what they say next, once (`HOLD_SECS`). Research: `docs/research/speaker-verification.md`. Each answered turn logs `[Latency]` lines (Pipecat's `UserBotLatencyObserver`, `enable_metrics` on; `latency.py` skips cycles with no released user turn) and `[Intent] ... ms` for the speech check, which Pipecat counts inside transcription. With `SAVE_SEGMENTS=N`, every segment Whisper gets (even one it returns no text for) is saved as a WAV in `JARVIS_HOME/clips` (folder 0700, files 0600, newest N kept) and named on its `[Speaker] score=` log line, to re-run Whisper and the voice check on real misses; off by default. Fixed phrases ("shut up", "you can talk") are caught by a local pattern; everything else goes to Laya's `jarvis-intent` (`POST $LAYA_BASE_URL/v1/systemone`, one `choice` question, fine-tuned for Jarvis, about 90 ms), which labels it `MUTE`, `UNMUTE`, `ADDRESSED_TO_JARVIS`, or `IGNORE_BACKGROUND`. The OpenRouter decision model `typesafe/jev-1.14` (`FILTER_MODEL`, `POST /api/alpha/decisions`, about 230 ms) gets the same question at the same time; its answer is used only when Laya fails or passes `LAYA_TIMEOUT_SECS`, and both answers go to `feedback/intent.jsonl` for retraining. A sentence that passes goes to `TranscriptCleaner` (`cleanup.py`, `TRANSCRIPT_CLEANUP`) before the `[Room]` / `[Speaker: ...]` / `[Meeting transcript:` labels are added: one OpenRouter call (`CLEANUP_MODEL`, thinking off, 1.5 s cap) fixes punctuation, drops fillers and resolves a slip fixed in the same sentence, and keeps an opening "No, sorry," so a correction still replaces the running task. Sentences of eight words or fewer skip it unless they hold a filler or a `CLEANUP_CUES` phrase; the speech check and its feedback log keep the words as heard; `[Cleanup] ... ms` logs the time.

### C. Agent Integration (Claude Code, OpenCode, the brain, T3 Code)
Instead of fragile terminal/tmux scraping, Jarvis connects via structured JSON protocols:
Each agent has three actions, one LLM tool each: `send_agent_task` (new work), `answer_agent` (reply to the question
or approval the agent is waiting on) and `stop_agent`. An answer never becomes a new prompt, and a task never answers
a pending question. With no agent named, a task goes to the default coding agent, an answer to the agent
that asked last, and a stop to the agent that is working. For every coding client (T3 Code, Claude Code, OpenCode,
Pi), `send_agent_task` also takes `session` (`"new"`, or the user's words naming an earlier session, matched against
session titles, branches and folders) and `project` (a listed folder: its newest session there that is not running,
or a new one), so the next task goes to that session, and its result names that session. `list_sessions` returns
a client's session titles, project, time, and which is running or current. A connected session (`jarvis connect`)
ignores `session` and `project`.

1. **Claude Code (`ClaudeCodeAdapter`):** One `ClaudeSDKClient` (Python `claude-agent-sdk`) per session, with
   the user's own Claude settings and permission mode (Jarvis sets none). It starts with `resume=<session id>`: the
   id from the CLI's `init` message, or the one `jarvis connect claude` picked from `list_sessions` (every folder,
   newest first). A crash restarts the process on the same id, and a crash mid-turn is said out loud. Picking or
   starting a session restarts the process, so neither happens while a turn runs. Tool permissions and `AskUserQuestion` arrive in
   `can_use_tool`; Jarvis reads them out and the agent waits for `answer_agent`. A turn ends with a `ResultMessage`.
   `stop_agent` calls `interrupt()` and denies any pending question.
2. **OpenCode (`OpenCodeSSEAdapter`):** Talks to the service OpenCode 2 registers in
   `~/.local/state/opencode/service.json` (`discover_service`, read before each connect since a restart picks a new
   port). The file is trusted only when it is the user's own, not a symlink, unreadable to others, names a loopback
   URL and a live pid. `OPENCODE_SERVER_PASSWORD` or a custom `url` pins a user-started `opencode serve` instead. Auth
   is HTTP Basic (`opencode` / password). Events come from `GET /api/event`; 45 s without bytes (heartbeats arrive
   every 15 s) means a dead stream and a reconnect. After each connect it re-reads `GET /api/permission/request` and
   `GET /api/form`, since events are not replayed. It surfaces `permission.asked`, `form.created` and the last text of
   each finished run, and drops a pending request on `permission.replied`, `form.replied` or `form.cancelled`.
   The shared service streams every session on the Mac, so Jarvis keeps to its `cwd`: listings carry
   `x-opencode-directory`, new sessions get `location.directory`, and an unknown session's events count only after
   one `GET /api/session/:id` shows it is in `cwd` (`_is_ours`); otherwise Jarvis could approve tool calls in other
   projects. Within `cwd` it follows one session: the first it sees, the one `jarvis connect opencode` picked from
   `GET /api/session` (which moves `cwd` to that session's folder), or a new one from `POST /api/session`. Events
   from other sessions (sub-agents run in child sessions) do not end the turn. Tasks go to that session
   (`POST /api/session/:id/prompt`); answers go to
   `.../permission/:requestID/reply` or `.../form/:formID/reply`; stop is `POST /api/session/:id/interrupt`.

3. **The brain (`BrainClientAdapter`, type `brain`):** The voice's one agent. It talks to Brain on
   `127.0.0.1:8643` (Bearer `BRAIN_API_KEY`, else `JARVIS_HOME/brain_token`) with the protocol below, which
   Brain serves from `apps/brain/src/brain/server.py`. A task is `POST /v1/runs` with `input` (and optionally
   `pinned`, `session`, `cwd`, `replace`, `screenshot`); later tasks pass the first run's id as `session_id`.
   Each run's `GET /v1/runs/:id/events` stream gives `approval.request` / `question.request` (read out; answered
   with `POST /v1/runs/:id/answer`, free text mapped deterministically), `session.switched` (the thread changed),
   and `run.completed` / `run.failed` (retold); `run.cancelled` is silent. If the stream drops,
   `GET /v1/runs/:id` gives the result. Stop is `POST /v1/runs/:id/stop`. `GET /v1/events` carries events no voice
   run is waiting on. Inside Brain, `BrainOrchestrator.route` picks the runner: a one-shot `pi` worker for a small
   question, a long-lived T3 thread for project work (auto-starting `t3 serve` on loopback), a minimal
   Claude/OpenCode turn when the owner names one, or the board. The reconciler still decides which board work waits
   for the owner. The whole contract is `schemas/openapi.json`.
4. **T3 Code (`T3Adapter` in the voice, `T3Runner` in the brain):** The normal path is `T3Runner` behind
   `POST /v1/runs`; the voice adapter remains for an `agents.json` that enables `t3-code` directly and for
   `jarvis connect`. Both talk to the running T3 Code app over `ws://127.0.0.1:3773/ws`
   (`?orchestrationProtocol=2`), the API the T3 window itself uses: Effect RPC messages as JSON (`Request`, `Exit`,
   `Chunk`, and an `Ack` for every chunk). Auth is a 30-day bearer token from
   `t3 auth session issue --scope orchestration:read --scope orchestration:operate`, issued once per run and again
   only after a 401/403. Its sessions are the user's threads in the T3 project holding a folder
   (`orchestration.subscribeShell`, `recent_threads`). The folder is `cwd`, or a `project` the conversation model
   picks from the git repos under `projects_dir` from `~/.config/jarvis/SOUL.md`, default `~/Projects/`
   (`list_projects`). The first task continues the newest thread there that is not running; with none, or after a
   new session, it starts a thread (`orchestration.launchThread`, `generateTitle`) with the user's T3 default model
   and runtime mode, whose first message is "Sync with the master branch first. Then: <task>". A project folder T3
   doesn't know yet is added with `project.create`. Jarvis watches one thread (picked, continued, started, or the
   one `jarvis connect` attached: `current_thread` from the T3 project script's folder env, else picked from
   `recent_threads`); a switch closes the old thread's stream with `Interrupt`. Tasks to it are `message.dispatch`
   with `deliveryIntent: "auto"`, like a send in the T3 window. `orchestration.subscribeThread`
   (resumed with `afterSequence`) gives `run.updated`, finished `assistant_message` items (retold when the run
   completes) and `approval_request` / `user_input_request` items (read out; answered with `runtime-request.respond`).
   Stop is `run.interrupt`. A 426 means T3 changed its protocol version.

### D. Executive Filter & Mute State Machine
1. **What reaches the user:** Watchers only pass on questions, approvals and finished turns. Questions and approvals
   are spoken as they are. A finished turn (long markdown) goes to the executive LLM as an `AGENT_REPORT_PROMPT`
   message, which retells it as one short first-person outcome per turn and calls no tool; approvals never go through the LLM, so it can't
   approve anything on its own. Offline, Jarvis reads the turn's last line. While a sent task runs,
   `remind_while_busy` says a short "still working on it" line now and then; a new task restarts the count. The
   lines are written per task by `EXECUTIVE_MODEL` (`waiting_lines.py`, `WAITING_PROMPT`) from the `languages.json`
   `waiting` lines and the user's task, 6 at a time, with the next batch started after 4 are spoken; the base lines
   are spoken as they are only when a call fails.
2. **Long-term memory (`memory.py`):** Pipecat's `Mem0MemoryService` sits just before the executive LLM. Before
   each reply it searches local Mem0 (Qdrant on disk, Ollama `bge-m3`, ~22 ms) and adds what it finds; after each
   reply it saves the newest exchange in the background, where `EXTRACTION_MODEL` (default `deepseek/deepseek-v4.1-flash`, thinking off) picks out the facts.
3. **Mute / Beep State Machine (`JarvisState`):**
   * **`VOICE` Mode:** Important agent topics are spoken immediately via TTS.
   * **`BEEP_ONLY` Mode (triggered by *"Shut up"*, *"Be quiet"*, *"Stop talking"*, *"Hush"*):** Active TTS is immediately cancelled, a low confirmation tone plays, and incoming agent topics are appended to `pending_topics` while playing a short 880Hz sine chime.
   * **Unmute Digest (triggered by *"You can talk"*):** Restores `VOICE` mode, reads out queued questions, then has the LLM retell queued finished turns.

### E. Brain timers, memory and outbox delivery
1. **Timers (`schedule.py`, `timers` table):** `jarvis remind`, `jarvis schedule`, voice phrasing ("remind me … in 30
   minutes", "every weekday at 8am …") and `POST /v1/timers` write one row: a one-shot `followup` or a five-field
   cron `schedule` read in its own IANA zone. `Reconciler.tick` advances a due row atomically (`firing`), creates a
   board task (`action='task'`) or starts a short read-only `pi` check beside the loop (`action='check'`), and returns
   a schedule to `active` at its next occurrence. A missed slot is skipped, never replayed in a burst; a crash is
   recovered after a grace period.
2. **Memory (`memory.py`):** `INSTRUCTIONS.md` is the owner's, read-only to workers; `memory/MEMORY.md` is a
   <=150-line index injected into every planner/builder run with `--append-system-prompt`; repository detail lives in
   `memory/repos/<slug>.md`. Progress notes now record verified results as well as failures. Only the offline
   Improver writes the two curated files (with `expected_sha256`), and its reflection pass corrects stale notes
   against the traces.
3. **Outbox delivery (`server.py`, `brain_client.py`):** in `ASSIST` mode a queued outbox row is published on
   `GET /v1/events`; the voice speaks it and acknowledges `POST /v1/outbox/{id}/spoken`, which writes one line to
   `memory/reported.md`. In `AUTONOMOUS` mode nothing is spoken until the owner switches back or asks for the brief.
   A reconnecting voice drains `GET /v1/outbox?status=queued` first, so a slept Mac does not lose the night's work.

---

## 4. Codebase Mapping

The `jarvis` command is in `src/jarvis/`; the voice files below are in `apps/voice/src/jarvis_voice/`.

| File                  | What it holds                                                              |
| :-------------------- | :------------------------------------------------------------------------- |
| `main.py`             | Pipeline wiring, TTS, `jarvis connect` thread and session pick             |
| `prompts.py`          | System prompt, agent roster, project list, retell and greeting prompts     |
| `languages.json`      | Per-language prompt rules and fixed spoken lines (`en`, `vi`)              |
| `agent_tools.py`      | `send_agent_task`, `answer_agent`, `stop_agent` schemas and handlers       |
| `cli.py`              | `jarvis` commands (init, service, HTTP calls), one-mic lock                 |
| `config.py`           | `Settings` from `.env`, `JARVIS_HOME`, the gateway host and port            |
| `soul.py`             | `SOUL.md` read and write                                                   |
| `system_prompts.py`   | `SYSTEM.md`: optional `## Name` sections that replace shipped prompts      |
| `init.py`             | `jarvis init`: copies the defaults, makes the brain folder and token, writes `SOUL.md` |
| `mac_audio.py`        | `MacVoiceTransport`: mic and speaker through macOS voice processing, handed to a paired Mac while it is connected |
| `voice_engine.py`     | `VoiceAudio`, `MicConverter`: that engine, without Pipecat (enroll, client) |
| `remote_audio.py`     | `RelayHub` + `RemoteVoiceTransport`: a paired Mac's mic and speaker over WS, always served |
| `defaults/remote/`    | `client.py`: what runs on that Mac; nothing but a mic, a resampler, a player |
| `voice_gates.py`      | `SpeakerVerifiedWhisperSTT`, `JarvisIntentGate`                            |
| `speaker.py`          | sherpa-onnx speaker embeddings, voiceprint load and save, model download   |
| `enroll.py`           | `jarvis enroll`: records the owner's voiceprint, sets `MASTER_CHECK=true`  |
| `executive_filter.py` | `ExecutiveFilter`: decision model, `INTENT_QUESTION`                       |
| `cleanup.py`          | `TranscriptCleaner`: tidies a passed sentence before the model reads it    |
| `state.py`            | `JarvisState` (`VOICE`, `BEEP_ONLY`, meeting mode), fast commands, beep synth |
| `agent_adapter.py`    | `BaseAgentAdapter`, `WatchedAgentConfig`, spoken answer to form, approval  |
| `sessions.py`         | `CodingSessionAdapter` (list, pick, new session); `match_sessions` from `jarvis_core` |
| `brain_client.py`     | `BrainClientAdapter`: the brain's runs, answers, stops, and session picker |
| `claude_adapter.py`   | `ClaudeCodeAdapter`: optional direct Claude Code (enabled in `agents.json`)  |
| `opencode_adapter.py` | `OpenCodeSSEAdapter`: optional direct OpenCode 2 service                    |
| `orchestrator.py`     | `AgentOrchestrator`, `ADAPTERS` (agents.json type to adapter), reminders   |
| `waiting_lines.py`    | `WaitingLines`: the per-task "still working" lines, refilled in batches    |
| `meeting_notes.py`    | `jarvis meeting` comments: the day's notes file, appended and read back     |
| `t3_adapter.py`       | `T3Adapter`, optional direct T3 Code, `jarvis connect` thread lookup       |
| `memory.py`           | Local Mem0 config, `LastExchangeMemory` (saves only the newest exchange)   |
