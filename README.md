# Jarvis — Always-On Voice Executive Harness

Jarvis is an always-on voice harness built on **Pipecat**, **MLX Whisper**, **sherpa-onnx**, the **Claude Agent SDK** and the **OpenCode v2 API**.

It listens continuously without a wake word and hands your requests to agents: coding to **T3 Code** (or **Claude Code** or **OpenCode** when you name it), everything else to **Hermes Agent**. It reads out their questions and approvals and retells each finished turn in plain words.

What it does:
- **Talks only with you.** It checks every sentence against your enrolled voice. Other people, the TV and its own
  voice are dropped, and they can't interrupt Jarvis or keep it waiting.
- **Answers only when you mean it.** A decision model tells talk aimed at Jarvis from talk around it.
- **Opens your projects by name.** "Continue working on the Hugger Hustle platform" starts a T3 Code thread in that
  repo that syncs with master; then you say what to do.
- **Stays out of the way.** A two-to-five-word greeting at startup, beeps instead of speech when you say "shut up",
  and a short digest when you say "you can talk".
- **Remembers you** between runs, locally (Mem0 + Qdrant + Ollama).

---

## Architecture

| Feature                           | Component                                   | File                  |
| :-------------------------------- | :------------------------------------------ | :-------------------- |
| No wake word, answer when needed  | Silero VAD, MLX Whisper, `JarvisIntentGate` | `voice_gates.py`      |
| Act only on your voice            | `SpeakerVerifiedWhisperSTT` (sherpa-onnx)   | `voice_gates.py`      |
| Background voices never interrupt | `ExternalUserTurnStrategies`                | `voice_gates.py`      |
| Don't hear its own voice          | `EchoFilter`, `EchoReference`               | `voice_gates.py`      |
| Hear only speech meant for Jarvis | `ExecutiveFilter` (decision model)          | `executive_filter.py` |
| "Shut up" / "You can talk"        | `JarvisState` (`VOICE`, `BEEP_ONLY`)        | `state.py`            |
| Claude Code, OpenCode, Hermes     | `*Adapter` classes                          | `agent_watchers.py`   |
| T3 Code, projects by name         | `T3Adapter`                                 | `t3_adapter.py`       |
| Memory                            | Mem0 + Qdrant + Ollama `bge-m3`             | `memory.py`           |

Files are in [`src/jarvis/`](src/jarvis/).

---

## Quick Start

### 1. Install Dependencies
```bash
uv sync
```

### 2. Configure `.env`
```bash
cp .env.example .env
```
Fill in:
- `OPENROUTER_API_KEY`: used for Jarvis's conversational brain (`EXECUTIVE_MODEL`, `deepseek/deepseek-v4.1-flash` with thinking off), which also retells finished agent turns in two sentences, and for the decision model that tells whether a sentence is meant for Jarvis (`FILTER_MODEL`, `typesafe/jev-1.14`). Left empty, Jarvis only hears "shut up" / "you can talk" and reads the last line of a finished turn.
- `DEFAULT_CODING_AGENT`: the exact `agents.json` id (e.g. `t3-code`) that gets coding tasks like *"build a login screen"* when you don't name an agent. Empty means the first enabled agent. An unknown id stops Jarvis at startup.
- `HERMES_API_KEY`: the same value as `API_SERVER_KEY` in `~/.hermes/.env`.
- `ELEVENLABS_API_KEY`: voice output with `eleven_v4_turbo` (about 100 ms to first audio; it laughs and sighs when the brain tags a reply).

### 3. Configure Watched Coding Agents
Edit [`agents.json`](agents.json): set each agent's `cwd` to the project it should work in. Each agent's `role` tells
Jarvis what to send it.
- **Claude Code** runs through the Agent SDK with your own Claude settings (`defaultMode`, allow rules), the same as
  in your terminal. Whatever those settings would ask you, and Claude's own questions, are read out, and Claude waits
  for your answer.
- **OpenCode** needs its v2 server running with the same password Jarvis has in `.env`:
  `OPENCODE_SERVER_PASSWORD=<same value> opencode serve --port 4096`, started in the project folder.
- **Hermes Agent** needs its API server: `API_SERVER_ENABLED=true` and an `API_SERVER_KEY` of 16+ characters in
  `~/.hermes/.env`, then `hermes gateway install` (port 8642). Hermes's own approval settings decide what it asks.
- **T3 Code** (the default) needs the T3 Code app running (port 3773). Jarvis gets its own token through the `t3`
  CLI. Say "continue working on the Hugger Hustle platform" and Jarvis picks the folder from your git repos one or
  two levels under `~/Projects/` (read at startup), adds it to T3 as a project if it isn't one, and starts a "Jarvis"
  thread there whose only first message is "Sync with the master branch." Tell it what to do next; later requests
  go to that thread. Coding with no project named runs in `cwd` from `agents.json`. Threads use your T3 default
  model and mode; Jarvis watches only its newest thread.

### 4. Memory
Jarvis remembers facts about you between runs (local [Mem0](https://github.com/mem0ai/mem0) with Qdrant in
`~/.jarvis/memory`). It needs the Ollama app running with the embedding model: `ollama pull bge-m3`. Without Ollama,
Jarvis still runs, just without memory.

### 5. Enroll Your Voice
Jarvis acts only on your voice and won't start until you enroll:
```bash
uv run jarvis-enroll
```
Read the three lines it shows, with the mic Jarvis will use. It downloads the speaker model (25 MB) to
`~/.jarvis/models/` and saves your voiceprint to `~/.jarvis/owner_voice.npy`. Anyone can still say "shut up" or
"you can talk". Enroll close to the mic in a quiet moment: background voices in the recording weaken the voiceprint.
Run it again any time to replace the voiceprint.

### 6. Run Jarvis
```bash
uv run jarvis
```
Jarvis greets you in a few words and listens. Use headphones for now. On speakers the mic also hears Jarvis, and
the current echo filter removes little of it: the voice check drops Jarvis's own sentences, but when you talk over
it, Whisper hears both voices.

### Tuning and Troubleshooting
- **Read the log first.** `[User Transcript]` is what Whisper heard; the lines after it show whether the sentence
  was passed on or ignored. T3 Code keeps terminal logs in `~/.t3/userdata/logs/terminals/*.log`.
- **Your voice gets dropped, or others get through.** Every sentence logs `[Speaker] score=0.71 pass`. Set
  `SPEAKER_THRESHOLD` in `.env` between your scores and everyone else's (default 0.5), or enroll again.
- **Jarvis splits one sentence into two.** A pause over 0.6 s ends a sentence; raise `stop_secs` in `main.py`.
- **Jarvis runs offline** ("OPENROUTER_API_KEY is not set") when it can't find `.env`: run it from the folder that
  has the file.

### Voice Commands
- **"Code [task]..." / "Build [feature]..." / "Fix [bug]..."** — Sends the task to your default coding agent.
- **"Continue working on [project]" / "Open [project]"** — Starts a T3 Code thread in that repo that syncs with master.
- **"Check my calendar" / "Email Minh..." / "Look up..."** — Sends it to Hermes.
- **Answering an agent** ("yes", "use Postgres") — Goes to the question or approval the agent is waiting on.
- **"Stop Claude"** / **"Stop that"** — Stops the agent's current turn.
- **"No, I meant..."** — Replaces a task an agent is still working on, instead of starting a second one.
- **Talking over Jarvis** — Cuts its reply off. Only your voice does this.
- **"Shut up"** / **"Be quiet"** — Immediately stops TTS playback and switches Jarvis into `BEEP_ONLY` mode. When a coding agent hits a blocker or needs a decision, Jarvis plays a short chime and queues the topic silently.
- **"You can talk"** — Switches Jarvis back to `VOICE` mode and immediately reads a concise, jargon-free digest of everything queued while muted.
