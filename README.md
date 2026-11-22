# Jarvis — Always-On Voice Executive Harness

> [!WARNING]
> Jarvis is at an early stage. Commands, settings and file formats (like `SOUL.md`) can change
> dramatically between versions, without a migration path.

Jarvis is an always-on voice harness built on **Pipecat**, **MLX Whisper**, **sherpa-onnx**, the **Claude Agent SDK** and the **OpenCode v2 API**.

It listens continuously without a wake word and hands your requests to agents: coding to **T3 Code** (or **Claude Code** or **OpenCode** when you name it), everything else to **the brain** (`brain`), the daemon it runs alongside itself: goals, research, notes, email, calendar. It reads out their questions and approvals and retells each finished turn in plain words.

Jarvis is one repository of three apps: the **voice layer** (`jarvis-voice`), the **brain** (`brain`, an
always-on daemon that starts with the voice) and the **cockpit** (`jarvis-top`, a Rust terminal dashboard over the
brain). Python and Rust share the workspace and the state home, and the layout leaves room for a Go service or a
React client without moving anything.

What it does:
- **Can talk only with you.** The master check is on by default: Jarvis checks every sentence against your voice
  and drops other people, the TV and its own voice, so they can't interrupt Jarvis or keep it waiting.
  `jarvis enroll` records the voiceprint; `MASTER_CHECK=false` lets any voice talk to it.
- **Answers only when you mean it.** A decision model tells talk aimed at Jarvis from talk around it.
- **Opens your projects by name.** "Continue working on the Hugger Hustle platform" picks up your newest T3 Code
  thread in that repo, or starts one that syncs with master, and tells you which. Questions about the list itself ("how many projects in
  OpsCom?") are answered right away, with no agent.
- **Stays out of the way.** A two-to-five-word greeting at startup, beeps instead of speech when you say "shut up",
  and a short digest when you say "you can talk". While an agent works, a short "still working on it" now and then,
  so you know the task isn't forgotten.
- **Remembers you** between runs, locally (Mem0 + Qdrant + Ollama).

Website: [sangdth.github.io/jarvis](https://sangdth.github.io/jarvis/) (source in [`site/`](site/)).

---

## Architecture

| Feature                           | Component                                   | File                  |
| :-------------------------------- | :------------------------------------------ | :-------------------- |
| No wake word, answer when needed  | Silero VAD, MLX Whisper, `JarvisIntentGate` | `voice_gates.py`      |
| Master check (opt-in)             | `SpeakerVerifiedWhisperSTT` (sherpa-onnx)   | `voice_gates.py`      |
| Background voices never interrupt | `ExternalUserTurnStrategies`                | `voice_gates.py`      |
| Don't hear its own voice          | `MacVoiceTransport` (voice processing)      | `mac_audio.py`        |
| Mic and speaker on another Mac    | `RemoteVoiceTransport`, `defaults/remote/`  | `remote_audio.py`     |
| Hear only speech meant for Jarvis | `ExecutiveFilter` (decision model)          | `executive_filter.py` |
| Tidy a sentence before the model  | `TranscriptCleaner` (`CLEANUP_MODEL`)       | `cleanup.py`          |
| "Shut up" / "You can talk"        | `JarvisState` (`VOICE`, `BEEP_ONLY`)        | `state.py`            |
| Claude Code, OpenCode, the brain  | `*Adapter` classes                          | `*_adapter.py`        |
| T3 Code, projects by name         | `T3Adapter`                                 | `t3_adapter.py`       |
| Memory                            | Mem0 + Qdrant + Ollama `bge-m3`             | `memory.py`           |
| Goals, problems, overnight work   | `Reconciler` + `pi` workers in worktrees    | `reconciler.py`       |
| Its own board, spend and approvals| SQLite WAL + one HTTP/SSE/WS gateway        | `server.py`           |
| Seeing what it is doing           | Rust + Ratatui cockpit                      | `apps/tui/`           |

The voice files above are in [`apps/voice/src/jarvis_voice/`](apps/voice/src/jarvis_voice/); the brain's are in
[`apps/brain/src/brain/`](apps/brain/src/brain/); the `jarvis` command itself is
[`src/jarvis/cli.py`](src/jarvis/cli.py).

### Repository layout

```
apps/voice/            jarvis-voice: Pipecat ear, speech check, executive model, agent watchers
apps/brain/            brain: board (SQLite), reconciler, pi workers, the HTTP/SSE/WS gateway
apps/tui/              jarvis-top: the Ratatui cockpit (Rust)
packages/jarvis-core/  the shared runtime home, .env settings base, tokens, SOUL.md
src/jarvis/            the `jarvis` command: init, the launcher, and the supervisor that runs it all
schemas/               the cross-language contracts (the gateway's HTTP/SSE/WS API)
scripts/               the speech and routing benchmarks
```

Each language keeps its own workspace: [uv](pyproject.toml) for Python, [Cargo](Cargo.toml) for Rust, and
`pnpm-workspace.yaml` / `go.work` when a client or service in another language arrives. [justfile](justfile) is the
one command runner (`just test`, `just lint`, `just build`), and [mise.toml](mise.toml) pins the toolchains. Each
contract lives in one place, listed in [schemas/README.md](schemas/README.md).

---

## Quick Start

### 1. Install
```bash
curl -fsSL https://sangdth.github.io/jarvis/install.sh | sh
```
The [script](site/install.sh) installs [uv](https://docs.astral.sh/uv/) if it is missing (which brings its own
Python, leaving yours alone), clones this repository into `~/Projects/jarvis` (set `JARVIS_DIR` to change that),
runs `uv sync`, and puts one tiny shim on your `PATH` that runs that checkout's `.venv`. If a Rust toolchain is
present it also builds the cockpit (`jarvis top`). Apple Silicon Macs only.

**There is no release to wait for: the checkout is the runtime.** Nothing is packaged into a wheel and nothing is
installed globally. Editing a file and running `jarvis restart` is the whole update loop; so is `git pull` and
`jarvis restart`. `jarvis --version` prints the branch and commit of the code that is actually running, with
`+dirty` when the tree has uncommitted edits.

Everything of yours — `.env`, `agents.json`, `SOUL.md`, the voiceprint, models, memory, the board, logs and the
tokens — lives in `JARVIS_HOME`, which is the checkout itself unless you set `JARVIS_HOME`, and is gitignored so
state and secrets never enter history.

**Developing Jarvis:** clone it, `uv sync`, `uv run jarvis`. Same code, same state folder, no separate "dev mode";
`uv run pytest` and `cargo test --workspace` cover the whole monorepo. One Jarvis listens per machine: a second one
stops at startup with "Jarvis is already running (pid ...)".

### 2. Set Up Jarvis
```bash
jarvis init
```
It creates the state home (`JARVIS_HOME`, the checkout by default) with a `.env` and an `agents.json` from
[`src/jarvis/defaults/`](src/jarvis/defaults/), the brain's own folder under it (`brain/`, with a board, worktrees and
skills) and a `brain_token` the voice, the brain, the cockpit and any thin client share; it never overwrites your
files. Moving over from a standalone Superbrain? `jarvis init` moves `~/.config/superbrain` to `JARVIS_HOME/brain`
for you. It asks for your OpenRouter and
ElevenLabs keys (typing stays hidden; Enter skips one) and saves them in `.env`, readable only by you; a key
already set, in `.env` or exported in your shell, is never asked. A shell key stays out of `.env`, so it must be
exported wherever Jarvis runs. Then it asks a few questions (Jarvis's personality, what to call you,
where you keep your projects, default `~/Projects`) and saves the answers to `JARVIS_HOME/SOUL.md`. Edit that
file by hand any time; Jarvis reads it at startup. The personality sets tone only, and the name stays Jarvis.

To change how Jarvis tidies what you say, write your own prompt under a `## Cleanup` heading in
`JARVIS_HOME/SYSTEM.md` (create the file; `init` doesn't). It replaces the shipped one; with no file or no section
Jarvis uses its own, and new versions keep improving it. Startup logs `SYSTEM.md overrides: Cleanup` while yours is used.

Last, it records your voice for the master check (on by default; later: `jarvis enroll`). Jarvis then acts only on
yours: read eight everyday lines (5 s each, in the language
`LANGUAGE` sets) with the mic Jarvis will use, then three more that Jarvis scores against your new voiceprint; it
prints those scores, suggests a `SPEAKER_THRESHOLD` just under the lowest and sets `MASTER_CHECK=true` in `.env`. It
downloads the speaker model (25 MB) to `JARVIS_HOME/models/` and saves your voiceprint to
`JARVIS_HOME/owner_voice.npy`. Anyone can still say "shut up" or "you can talk". Enroll close to the mic in a
quiet moment: background voices in the recording weaken the voiceprint. Declining the recording writes
`MASTER_CHECK=false`, and any voice may talk to Jarvis until you enroll; `MASTER_CHECK=false` also turns the check
off again later and keeps the voiceprint.

### 3. Configure `.env`
`jarvis init` fills the two API keys unless your shell exports them; set the rest in `JARVIS_HOME/.env`. A variable set in
your shell wins over the file, so `op run -- jarvis` (1Password) works too:
- `OPENROUTER_API_KEY`: used for Jarvis's conversational brain (`EXECUTIVE_MODEL`, `deepseek/deepseek-v4.1-flash`, thinking on), which also retells each finished agent turn as one short first-person outcome ("I pushed it, but there was nothing for a PR"), and for the fallback speech check (`FILTER_MODEL`, `typesafe/jev-1.14`), and for picking memory facts out of each exchange (`EXTRACTION_MODEL`, a fast non-thinking model). Left empty, Jarvis only hears "shut up" / "you can talk" and reads the last line of a finished turn.
- `LAYA_API_KEY`: a console key (`laya_...`, from https://laya.happyvoxel.com, valid 90 days) for the speech check
  that tells whether a sentence is meant for Jarvis: Laya's `jarvis-intent` model, fine-tuned for Jarvis. `FILTER_MODEL`
  is asked at the same time and used when Laya fails or is slower than `LAYA_TIMEOUT_SECS`; left empty, only
  `FILTER_MODEL` is used.
- `TRANSCRIPT_CLEANUP` (default `true`): each sentence meant for Jarvis — over 18 words, or with a filler or a
  phrase from `CLEANUP_CUES` — is tidied before Jarvis reads it: punctuation, fillers, and a slip fixed in the same
  sentence ("make it 42, sorry, 24" becomes "Make it 24"). It uses `CLEANUP_MODEL` (empty = `EXECUTIVE_MODEL`,
  thinking off), adds about 250-500 ms when it runs, and passes the sentence on as heard after 1.5 s or on any error.
  `[Cleanup]` log lines show the time and the change.
- `DEFAULT_CODING_AGENT`: the exact `agents.json` id that gets work when you don't name an agent. The shipped roster has one, `brain`; empty means the first enabled agent. An unknown id stops Jarvis at startup.
- `LANGUAGE`: `en` (default) or `vi`. It sets the language Whisper listens for and, while `WHISPER_MODEL` is empty, the model: `mlx-community/whisper-large-v3-turbo` for `en`, [`sangdth/phowhisper-large-vien-cs-mlx-16bit`](https://huggingface.co/sangdth/phowhisper-large-vien-cs-mlx-16bit) for `vi`: VinAI's PhoWhisper-large tuned for Vietnamese with English words mixed in, about 3 GB, downloaded on first start. It takes about twice as long per sentence as turbo.
- `ELEVENLABS_API_KEY`: voice output with `eleven_v4_turbo` (about 100 ms to first audio; it laughs and sighs when the brain tags a reply).

### 4. Configure Watched Coding Agents
Edit `JARVIS_HOME/agents.json`. Each agent's `role` tells Jarvis
what to send it. A `cwd` of `.` means the folder you start Jarvis in; set a path to pin an agent to one project.
- **Claude Code** runs through the Agent SDK with your own Claude settings (`defaultMode`, allow rules), the same as
  in your terminal. Whatever those settings would ask you, and Claude's own questions, are read out, and Claude waits
  for your answer.
- **OpenCode** 2 runs its own service, and Jarvis finds it (`~/.local/state/opencode/service.json`); its sessions
  go in the agent's `cwd`, and requests from other projects are ignored. To use a server of your own instead, start
  it with `OPENCODE_SERVER_PASSWORD=<same value as in .env> opencode serve --port 4096` in the project folder.
- **The brain** (the only agent in the shipped roster) needs no setup: it always runs with the voice, in
  the same process, and its HTTP/SSE/WebSocket gateway is the only network surface Jarvis exposes. It routes each
  task itself: a small question to a one-shot `pi` worker, project work to a T3 Code thread, a named Claude Code or
  OpenCode turn to that client, and anything for later to its board. Its board, spend and skills:
  `jarvis status`; watch it live: `jarvis top`.
- **T3 Code** is the coding surface the brain drives: the owner sees the thread, the diff and the review. The brain
  starts `t3 serve --mode desktop` on loopback itself when nothing answers on port 3773, and gets its own token
  through the `t3` CLI. Say "continue working on the Hugger Hustle platform" and the brain picks the folder from your
  git repos one or two levels under your projects folder (`~/Projects/` unless `SOUL.md` says otherwise; read at
  startup) and continues your newest thread there that isn't running. With none, or when you ask for a new session,
  your next
  task starts a thread (adding the folder to T3 as a project if needed) that syncs with master first, then does the
  task; T3 titles it. Coding with no project named goes to the most recently used project. Threads use your T3
  default model and mode; the brain can watch several threads at once and says a title when the work moved.

### 5. Memory
Jarvis remembers facts about you between runs (local [Mem0](https://github.com/mem0ai/mem0) with Qdrant in
`JARVIS_HOME/memory`). It needs the Ollama app running with the embedding model: `ollama pull bge-m3`. Without
Ollama, Jarvis still runs, just without memory.

### 6. Run Jarvis
```bash
jarvis                 # listen here, in this terminal
jarvis start           # or as a 24/7 service: launchd keeps this checkout running
jarvis restart         # after any edit: sync if dependencies changed, then restart on the new code
```
Jarvis greets you in a few words and listens. Speakers work: the mic and speaker run through macOS voice
processing (the echo canceller FaceTime uses), so you can talk over Jarvis. With the master check on, enroll your
voice again after updating from an older version (`jarvis enroll`): it now records through the same processing.
Updated from a version that required enrollment? Your voiceprint stays, but the check is now off until you set
`MASTER_CHECK=true`; the startup log says so.

`jarvis restart` never waits for a release: the daemon runs this checkout directly, so the file you just saved is
the code that starts. `jarvis logs` follows its log; `jarvis stop` stops it.

### The brain and the cockpit

The brain is the part of Jarvis that keeps working when you stop talking: it holds a board of goals and problems,
picks one when it is allowed to (mode `AUTONOMOUS`, under a daily budget), and runs a `pi` worker in a clean worktree
on a branch of its own — never merging to master on its own. In `ASSIST` mode it only does what you ask for.

```bash
jarvis status                             # version, uptime, mode, spend today, tasks, voice
jarvis tasks                              # the board
jarvis add --repo jarvis --kind goal --title "..." --spec "..." --ready
jarvis mode AUTONOMOUS                    # or ASSIST: pause background work
jarvis top                                # live cockpit: tasks, attempts, spend, telemetry (cargo build first)
jarvis restart                            # the 24/7 service, on this checkout
```

Everything above is an HTTP call to the one server, so the same commands work from another machine:
`jarvis status --url http://mac-mini.local:8643`. Both apps share one state: `.env`, `SOUL.md` (its `projects_dir`
decides where the brain may work), the board, and the token in `JARVIS_HOME/brain_token`.

### Listen from another Mac (thin client, no install)
```bash
jarvis remote          # on the Mac mini: serve the mic and speaker of another machine
jarvis pair            # show a one-time code for a laptop to pair with
```
`jarvis pair` prints exactly what to paste on the other Mac, which needs nothing but
[uv](https://docs.astral.sh/uv/):
```bash
curl -fsSL 'http://<mac-mini>:8640/client.py' -o ~/.local/bin/jarvis-remote.py
uv run ~/.local/bin/jarvis-remote.py --pair 123456
```
The code is single-use and good for five minutes; the laptop trades it for **its own secret**, kept in
`~/.config/jarvis-remote/credentials.json` (0600). From then on `uv run ~/.local/bin/jarvis-remote.py` needs no
arguments, and no token appears in a URL, a shell history or a log. Every model, agent, key and memory stays on the
Mac mini; uv fetches four small packages (numpy, soxr, websockets, pyobjc's AVFoundation) into a cache. Ctrl+C
stops it.

- **Talk over it, and "shut up" still works.** The mic runs through macOS voice processing on the Mac in front of
  you, so the echo canceller that lets you interrupt Jarvis sits where the speaker is. Its speech plays on that
  Mac's speakers; an interruption drops what it still had queued.
- **One listener at a time.** A second Mac is refused while one is connected: they share one ear.
- **Walk between Macs.** Set `RELAY_EARS=true` and run `jarvis` rather than `jarvis remote`: this Mac keeps its own
  mic and speaker, another Mac joins over the relay, and the ear follows whichever one hears you. A client that
  connects takes the ear at once.
- **Each device has its own key.** `jarvis devices` lists them; `jarvis devices revoke <id>` cuts one off
  (a lost laptop) without touching the others or the CLI. Only the machine running Jarvis — the shared token — can
  pair or revoke.
- **Same Wi-Fi.** Plain HTTP and WebSocket on the LAN. Away from that, put both Macs on Tailscale and run `curl`
  against that address instead; the pairing secret never travels in the URL.
- **The master check** compares your voiceprint, recorded with the other Mac's mic, against this one's. It usually
  still passes; watch the `[Speaker] score=` lines and lower `SPEAKER_THRESHOLD` if your own sentences are dropped.
- **The screen is the Mac's.** `look_at_screen` captures the front window of the Mac running Jarvis.
- **Agents, sessions, threads** work as usual: `jarvis remote connect` continues a T3 Code thread, picked there.

### Any client you want

The gateway is documented in [`schemas/openapi.json`](schemas/openapi.json), so a phone Shortcut, a web page, or a
Go service can drive Jarvis without any of this code:

```bash
curl -H "Authorization: Bearer $TOKEN" http://mac-mini.local:8643/health         # no token needed
curl -H "Authorization: Bearer $TOKEN" http://mac-mini.local:8643/v1/status
curl -H "Authorization: Bearer $TOKEN" http://mac-mini.local:8643/v1/board
curl -H "Authorization: Bearer $TOKEN" -X POST http://mac-mini.local:8643/v1/restart
```

### Continue a T3 Code thread away from the keyboard
```bash
jarvis connect
```
Add it once as a project script in T3 Code (Settings, default project scripts, command `jarvis connect`), then run
it from the thread you are in: Jarvis continues that thread. T3 tells a script only its folder, so a thread in its
own worktree is found exactly; on a project's root folder, Jarvis takes the thread you viewed last there. It says
"Connected to *title*" so you know which one. Run in a normal terminal, it lists your 15 newest threads (the ones
you started; imports and agent sub-threads are left out) to pick from by number.

Once connected, what you say goes to that thread, a question or approval it is waiting on is read out right away,
and its finished turns are retold. Nothing is sent to the thread when you connect. Stop Jarvis (Ctrl+C) when you
are back at the keyboard, or it reads out the replies to what you type too. As a project script, Jarvis runs
inside the T3 Code app, so macOS asks whether T3 Code may use the microphone; allow it.

### Continue a Claude Code or OpenCode session
```bash
jarvis connect claude      # or: jarvis connect opencode
```
It lists that agent's recent sessions in every folder, newest first, by folder and title. Pick one by number and
what you say goes to it, in its own folder; finished turns are retold. Claude Code needs the `claude` CLI on your
`PATH`; OpenCode needs its service running (step 4 above). Nothing is sent when you connect.

### Talk to an open Pi window
```bash
jarvis connect pi
```
The first run puts Jarvis's Pi extension in `~/.pi/agent/extensions/jarvis.ts` and stops: run `/reload` in your open
Pi windows (new windows load it on their own), then run it again. It lists the Pi windows open now, newest first, by
folder and session name. Pick one, and what you say goes into that window as if you typed it; finished turns are
retold. Jarvis can't answer a dialog in the window (an extension's confirm or select): it tells you one is waiting,
and you answer it at the keyboard. A saved session with no window open isn't listed; open `pi` in it first.

### Sit in a Google Meet call
```bash
jarvis meeting               # or: jarvis connect --meeting
```
Load the Chrome extension once from the source: `chrome://extensions`, turn on Developer mode, **Load unpacked**,
pick `apps/voice/src/jarvis_voice/defaults/meet` (the daemon logs its own copy's path on start). After a change to it,
press its reload button there. Then open the call in Chrome (reload a tab that was already open). Keep your usual mic in
Meet: the extension mixes Jarvis's voice into it, so the call hears you both, and muting yourself in Meet mutes
Jarvis too. If Chrome asks whether Meet may reach apps on this device, allow it: that is Jarvis's voice socket
(`127.0.0.1:47123`, Meet's page only). Wear headphones: on speakers the mic hears the call a second time. Jarvis
hears the call through macOS's system audio tap (macOS 14.2 or later), so allow your terminal under System
Settings > Privacy & Security > Screen & System Audio Recording. People on the call talk to each other until
someone says "Jarvis"; Jarvis then gets what was said since. Meeting talk is never saved to memory.

Who may do what depends on the master check. On (the default): guests may ask questions and ask for work, and
Jarvis asks you before it starts that work (`MEETING_GUESTS=ask`, the default); `MEETING_GUESTS=allow` starts it at
once. Either way only your voice can answer an agent's question, approve, stop or replace a task. Off: Jarvis can't
tell voices apart, so everyone counts as you.

**Meeting mode can be switched by voice, mid-session:** "meeting mode on" enters it (guests are tagged, comments
work, the call audio tap starts) and "meeting mode off" leaves it, without restarting Jarvis. Only you can switch it
when the master check is on. `jarvis meeting` at startup is just the same thing from the command line.

**Anyone may add a comment**, and that is the one thing a guest can do freely: "Jarvis, note that we're going with
Postgres." It goes to today's notes file with who said it and when —
`JARVIS_HOME/meeting/2026-11-22.md` — Jarvis acknowledges it in a few words, and nothing runs, nothing is approved,
and it never reaches memory. Ask "what did we note?" and it summarizes the day so far. The file is plain Markdown:
read it, edit it, or delete it.

With the master check on you can also introduce someone: "Jarvis, this is David." Jarvis chats with the new voice
for two questions, learns the name, and next time greets them by name and treats them as a guest. Off, it just
says hello. Profiles live in `JARVIS_HOME/speakers/`; delete a file there to forget a voice.

### Tuning and Troubleshooting
- **Read the log first.** `[User Transcript]` is what Whisper heard; the lines after it show whether the sentence
  was passed on or ignored. T3 Code keeps terminal logs in `~/.t3/userdata/logs/terminals/*.log`.
- **Your voice gets dropped, or others get through.** With the master check on, every sentence logs
  `[Speaker] score=0.71 pass` (off: `[Speaker] off pass`). Set
  `SPEAKER_THRESHOLD` in `.env` between your scores and everyone else's (default 0.35), or enroll again.
- **Jarvis splits one sentence into two.** Smart Turn waits up to 1.5 s when your voice sounds unfinished; with
  `SMART_TURN=false` a pause over 0.6 s ends a sentence (raise `stop_secs` in `main.py`).
- **Jarvis can't see your screen.** Allow the app running Jarvis in System Settings > Privacy & Security > Screen
  Recording; it looks only when you point ("this error", "on my screen").
- **Jarvis runs offline** ("OPENROUTER_API_KEY is not set") when `JARVIS_HOME/.env` is missing or has no
  key: run `jarvis init` again; it asks only for the keys still empty. A key that lives only in your shell is
  missing when Jarvis starts from launchd, which does not load your shell profile: put it in `.env` instead.
- **`jarvis remote`: the other Mac hears or plays nothing.** Jarvis logs `Relay client connected`; if it logs
  `allow Microphone` three seconds later, the terminal there has no Microphone permission. `refused ... (bad token)`
  means the URL is from an older run: paste the command `jarvis remote` prints now. `Client refused (1013)` on the
  other Mac means a client is already connected there.
### Voice Commands
- **"Code [task]..." / "Build [feature]..." / "Fix [bug]..."** — Sends the task to your default coding agent.
- **"Continue working on [project]" / "Open [project]"** — Continues your newest T3 Code thread in that repo.
- **"What sessions do I have?" / "Name the threads in [project]"** — Reads out session titles, newest first.
- **"Start a new session" / "Go back to the thread about [topic]" / "On OpenCode, continue the session about
  [topic]"** — Starts a clean session, or goes back to the earlier one whose title, branch or folder matches.
- **"Check my calendar" / "Email Minh..." / "Look up..."** — Sends it to Brain.
- **Answering an agent** ("yes", "use Postgres") — Goes to the question or approval the agent is waiting on.
- **"Stop it"** / **"Stop that"** / **"Cancel that"** / **"Stop Claude"** — Stops the agent's current turn; with
  nothing running, Jarvis says so. "Stop" stops the work, "shut up" mutes Jarvis.
- **"No, I meant..."** — Replaces a task an agent is still working on, instead of starting a second one.
- **Talking over Jarvis** — Cuts its reply off. With the master check on, only your voice does this.
- **Vietnamese** — Set `LANGUAGE=vi` and speak Vietnamese, with English words mixed in; Jarvis answers in Vietnamese and still briefs agents in English. Agent questions and approvals are read out in English.
- **"Meeting mode on" / "Meeting mode off"** — enter or leave meeting mode by voice, without restarting Jarvis.
  In meeting mode other voices are tagged `[Room]`, may ask questions and add comments, and only you can start,
  approve or stop work. Only your voice can switch it.
- **"Shut up"** / **"Be quiet"** / **"Stop talking"** / **"Hush"** / **"Im đi"** / **"Im lặng"** — Immediately stops TTS playback and switches Jarvis into `BEEP_ONLY` mode. When a coding agent hits a blocker or needs a decision, Jarvis plays a short chime and queues the topic silently.
- **"You can talk"** / **"Nói được rồi"** — Switches Jarvis back to `VOICE` mode and immediately reads a concise, jargon-free digest of everything queued while muted.

## License

[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0-only).
