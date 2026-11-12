# Jarvis — Always-On Voice Executive Harness

> [!WARNING]
> Jarvis is at an early stage. Commands, settings and file formats (like `~/.config/jarvis/SOUL.md`) can change
> dramatically between versions, without a migration path.

Jarvis is an always-on voice harness built on **Pipecat**, **MLX Whisper**, **sherpa-onnx**, the **Claude Agent SDK** and the **OpenCode v2 API**.

It listens continuously without a wake word and hands your requests to agents: coding to **T3 Code** (or **Claude Code** or **OpenCode** when you name it), everything else to **Hermes Agent**. It reads out their questions and approvals and retells each finished turn in plain words.

What it does:
- **Can talk only with you.** Turn on the master check (`MASTER_CHECK=true`, set by `jarvis enroll`) and it checks
  every sentence against your voice. Other people, the TV and its own voice are dropped, and they can't interrupt
  Jarvis or keep it waiting. Off by default: any voice may talk to it.
- **Answers only when you mean it.** A decision model tells talk aimed at Jarvis from talk around it.
- **Opens your projects by name.** "Continue working on the Hugger Hustle platform" starts a T3 Code thread in that
  repo that syncs with master; then you say what to do. Questions about the list itself ("how many projects in
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
| Hear only speech meant for Jarvis | `ExecutiveFilter` (decision model)          | `executive_filter.py` |
| "Shut up" / "You can talk"        | `JarvisState` (`VOICE`, `BEEP_ONLY`)        | `state.py`            |
| Claude Code, OpenCode, Hermes     | `*Adapter` classes                          | `*_adapter.py`        |
| T3 Code, projects by name         | `T3Adapter`                                 | `t3_adapter.py`       |
| Memory                            | Mem0 + Qdrant + Ollama `bge-m3`             | `memory.py`           |

Files are in [`src/jarvis/`](src/jarvis/).

---

## Quick Start

### 1. Install
```bash
curl -fsSL https://sangdth.github.io/jarvis/install.sh | sh
```
The [script](site/install.sh) installs [uv](https://docs.astral.sh/uv/) if it is missing, which brings its own Python
(your own Python is left alone), runs `uv tool install --python 3.12 "git+https://github.com/sangdth/jarvis@master"`,
then `jarvis init`. Apple Silicon Macs only. This puts the `jarvis` command on your PATH, so it runs from any folder. `jarvis update` moves it to the newest
master and prints the commit it went from and to; `jarvis --version` shows the one you have (`master @ 0fbc724`).
Everything of yours lives in `~/.config/jarvis/`, never in the folder you run it from. `jarvis uninstall` deletes that
folder and removes the `jarvis` command; it asks first and refuses while Jarvis is listening.

**Developing Jarvis:** in a checkout, `uv sync`, then `uv run jarvis` runs that checkout's code
(`jarvis --version` there says `dev (<folder>)`). It keeps its own state in the checkout, not `~/.config/jarvis`:
`.env`, `agents.json`, `SOUL.md`, `owner_voice.npy`, `models/` and `memory/` (all gitignored), and it stops at startup
without its `.env`. Fill them with `uv run jarvis init`, or copy them in with a T3 setup script for new worktrees:
```sh
set -e
for f in .env agents.json SOUL.md owner_voice.npy models; do
  [ -e "$f" ] || cp -R "$T3CODE_PROJECT_ROOT/$f" "$f"
done
uv sync
```
Dev and installed still share one mic, so only one runs at a time: a second one stops at startup with "Jarvis is
already running (pid ...)".

### 2. Set Up Jarvis
```bash
jarvis init
```
It creates `~/.config/jarvis/` with a `.env` and an `agents.json` from
[`src/jarvis/defaults/`](src/jarvis/defaults/); it never overwrites them. It asks for your OpenRouter and
ElevenLabs keys (typing stays hidden; Enter skips one) and saves them in `.env`, readable only by you; a key
already set is never asked again. Then it asks a few questions (Jarvis's personality, what to call you,
where you keep your projects, default `~/Projects`) and saves the answers to `~/.config/jarvis/SOUL.md`. Edit that
file by hand any time; Jarvis reads it at startup. The personality sets tone only, and the name stays Jarvis.

Last, it offers to record your voice for the master check (default no; later: `jarvis enroll`). Without it, Jarvis
acts on any voice it hears. With it, Jarvis acts only on yours: read eight everyday lines (5 s each, in the language
`LANGUAGE` sets) with the mic Jarvis will use, then three more that Jarvis scores against your new voiceprint; it
prints those scores, suggests a `SPEAKER_THRESHOLD` just under the lowest and sets `MASTER_CHECK=true` in `.env`. It
downloads the speaker model (25 MB) to `~/.config/jarvis/models/` and saves your voiceprint to
`~/.config/jarvis/owner_voice.npy`. Anyone can still say "shut up" or "you can talk". Enroll close to the mic in a
quiet moment: background voices in the recording weaken the voiceprint. `MASTER_CHECK=false` turns the check off
again and keeps the voiceprint.

### 3. Configure `.env`
`jarvis init` fills the two API keys; set the rest in `~/.config/jarvis/.env`. A variable set in
your shell wins over the file, so `op run -- jarvis` (1Password) works too:
- `OPENROUTER_API_KEY`: used for Jarvis's conversational brain (`EXECUTIVE_MODEL`, `deepseek/deepseek-v4.1-flash`, thinking on), which also retells each finished agent turn as one short first-person outcome ("I pushed it, but there was nothing for a PR"), and for the fallback speech check (`FILTER_MODEL`, `typesafe/jev-1.14`), and for picking memory facts out of each exchange (`EXTRACTION_MODEL`, a fast non-thinking model). Left empty, Jarvis only hears "shut up" / "you can talk" and reads the last line of a finished turn.
- `LAYA_API_KEY`: a console key (`laya_...`, from https://laya.happyvoxel.com, valid 90 days) for the speech check
  that tells whether a sentence is meant for Jarvis: Laya's `jarvis-intent` model, fine-tuned for Jarvis. `FILTER_MODEL`
  is asked at the same time and used when Laya fails or is slower than `LAYA_TIMEOUT_SECS`; left empty, only
  `FILTER_MODEL` is used.
- `DEFAULT_CODING_AGENT`: the exact `agents.json` id (e.g. `t3-code`) that gets coding tasks like *"build a login screen"* when you don't name an agent. Empty means the first enabled agent. An unknown id stops Jarvis at startup.
- `HERMES_API_KEY`: the same value as `API_SERVER_KEY` in `~/.hermes/.env`.
- `LANGUAGE`: `en` (default) or `vi`. It sets the language Whisper listens for and, while `WHISPER_MODEL` is empty, the model: `mlx-community/whisper-large-v3-turbo` for `en`, [`sangdth/phowhisper-large-vien-cs-mlx-16bit`](https://huggingface.co/sangdth/phowhisper-large-vien-cs-mlx-16bit) for `vi`: VinAI's PhoWhisper-large tuned for Vietnamese with English words mixed in, about 3 GB, downloaded on first start. It takes about twice as long per sentence as turbo.
- `ELEVENLABS_API_KEY`: voice output with `eleven_v4_turbo` (about 100 ms to first audio; it laughs and sighs when the brain tags a reply).

### 4. Configure Watched Coding Agents
Edit `~/.config/jarvis/agents.json`. Each agent's `role` tells Jarvis
what to send it. A `cwd` of `.` means the folder you start Jarvis in; set a path to pin an agent to one project.
- **Claude Code** runs through the Agent SDK with your own Claude settings (`defaultMode`, allow rules), the same as
  in your terminal. Whatever those settings would ask you, and Claude's own questions, are read out, and Claude waits
  for your answer.
- **OpenCode** 2 runs its own service, and Jarvis finds it (`~/.local/state/opencode/service.json`); its sessions
  go in the agent's `cwd`, and requests from other projects are ignored. To use a server of your own instead, start
  it with `OPENCODE_SERVER_PASSWORD=<same value as in .env> opencode serve --port 4096` in the project folder.
- **Hermes Agent** needs its API server: `API_SERVER_ENABLED=true` and an `API_SERVER_KEY` of 16+ characters in
  `~/.hermes/.env`, then `hermes gateway install` (port 8642). Hermes's own approval settings decide what it asks.
- **T3 Code** (the default) needs the T3 Code app running (port 3773). Jarvis gets its own token through the `t3`
  CLI. Say "continue working on the Hugger Hustle platform" and Jarvis picks the folder from your git repos one or
  two levels under your projects folder (`~/Projects/` unless `SOUL.md` says otherwise; read at startup), adds it to T3 as a project if it isn't one, and starts a "Jarvis"
  thread there whose only first message is "Sync with the master branch." Tell it what to do next; later requests
  go to that thread. Coding with no project named runs in `cwd` from `agents.json`. Threads use your T3 default
  model and mode; Jarvis watches only its newest thread.

### 5. Memory
Jarvis remembers facts about you between runs (local [Mem0](https://github.com/mem0ai/mem0) with Qdrant in
`~/.config/jarvis/memory`). It needs the Ollama app running with the embedding model: `ollama pull bge-m3`. Without
Ollama, Jarvis still runs, just without memory.

### 6. Run Jarvis
```bash
jarvis
```
Jarvis greets you in a few words and listens. Speakers work: the mic and speaker run through macOS voice
processing (the echo canceller FaceTime uses), so you can talk over Jarvis. With the master check on, enroll your
voice again after updating from an older version (`jarvis enroll`): it now records through the same processing.
Updated from a version that required enrollment? Your voiceprint stays, but the check is now off until you set
`MASTER_CHECK=true`; the startup log says so.

### Continue a T3 Code thread away from the keyboard
```bash
jarvis connect
```
Add it once as a project script in T3 Code (Settings, default project scripts, command `jarvis connect`), then run
it from the thread you are in: Jarvis continues that thread. While developing Jarvis, give the jarvis project its own
script, `uv run jarvis connect`: T3 runs it in the thread's worktree, so it runs that worktree's code. T3 tells a script only its folder, so a thread in its
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
brew install blackhole-2ch   # once: the virtual mic the call hears
jarvis meeting               # or: jarvis connect --meeting
```
In Google Meet, set the microphone to **BlackHole 2ch**; Jarvis sends your voice and its own into it. Keep the
speakers on your normal output and wear headphones: on speakers the mic hears the call a second time. Jarvis hears
the call through macOS's system audio tap (macOS 14.2 or later), so allow your terminal under System Settings >
Privacy & Security > Screen & System Audio Recording. People on the call talk to each other until someone says
"Jarvis"; Jarvis then gets what was said since. Meeting talk is never saved to memory.

Who may do what depends on the master check. Off: Jarvis can't tell voices apart, so everyone counts as you. On:
guests may ask questions and ask for work, and Jarvis asks you before it starts that work (`MEETING_GUESTS=ask`,
the default); `MEETING_GUESTS=allow` starts it at once. Either way only your voice can answer an agent's question,
approve, stop or replace a task.
With the master check on you can also introduce someone: "Jarvis, this is David." Jarvis chats with the new voice
for two questions, learns the name, and next time greets them by name and treats them as a guest. Off, it just
says hello. Profiles live in `~/.config/jarvis/speakers/`; delete a file there to forget a voice.
`MEETING_VIRTUAL_MIC` in `.env` picks another virtual mic by name.

### Tuning and Troubleshooting
- **Read the log first.** `[User Transcript]` is what Whisper heard; the lines after it show whether the sentence
  was passed on or ignored. T3 Code keeps terminal logs in `~/.t3/userdata/logs/terminals/*.log`.
- **Your voice gets dropped, or others get through.** With the master check on, every sentence logs
  `[Speaker] score=0.71 pass` (off: `[Speaker] off pass`). Set
  `SPEAKER_THRESHOLD` in `.env` between your scores and everyone else's (default 0.35), or enroll again.
- **Jarvis splits one sentence into two.** Smart Turn waits up to 1.5 s when your voice sounds unfinished; with
  `SMART_TURN=false` a pause over 0.6 s ends a sentence (raise `stop_secs` in `main.py`).
- **Jarvis runs offline** ("OPENROUTER_API_KEY is not set") when `~/.config/jarvis/.env` is missing or has no
  key: run `jarvis init` again; it asks only for the keys still empty.

### Voice Commands
- **"Code [task]..." / "Build [feature]..." / "Fix [bug]..."** — Sends the task to your default coding agent.
- **"Continue working on [project]" / "Open [project]"** — Starts a T3 Code thread in that repo that syncs with master.
- **"Start a new Claude Code session" / "On OpenCode, continue the session about [topic]"** — Starts a clean
  conversation, or goes back to the earlier session whose title, branch or folder matches what you said.
- **"Check my calendar" / "Email Minh..." / "Look up..."** — Sends it to Hermes.
- **Answering an agent** ("yes", "use Postgres") — Goes to the question or approval the agent is waiting on.
- **"Stop it"** / **"Stop that"** / **"Cancel that"** / **"Stop Claude"** — Stops the agent's current turn; with
  nothing running, Jarvis says so. "Stop" stops the work, "shut up" mutes Jarvis.
- **"No, I meant..."** — Replaces a task an agent is still working on, instead of starting a second one.
- **Talking over Jarvis** — Cuts its reply off. With the master check on, only your voice does this.
- **Vietnamese** — Set `LANGUAGE=vi` and speak Vietnamese, with English words mixed in; Jarvis answers in Vietnamese and still briefs agents in English. Agent questions and approvals are read out in English.
- **"Shut up"** / **"Be quiet"** / **"Stop talking"** / **"Hush"** / **"Im đi"** / **"Im lặng"** — Immediately stops TTS playback and switches Jarvis into `BEEP_ONLY` mode. When a coding agent hits a blocker or needs a decision, Jarvis plays a short chime and queues the topic silently.
- **"You can talk"** / **"Nói được rồi"** — Switches Jarvis back to `VOICE` mode and immediately reads a concise, jargon-free digest of everything queued while muted.

## License

[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0-only).
