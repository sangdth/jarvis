# Jarvis — Always-On Voice Executive Harness

Jarvis is an always-on voice harness built on **Pipecat**, **MLX Whisper**, the **Claude Agent SDK** and the **OpenCode v2 API**.

It listens continuously without a wake word and hands your requests to agents: coding to **T3 Code** (or **Claude Code** or **OpenCode** when you name it), everything else to **Hermes Agent**. It reads out their questions and approvals and retells each finished turn in plain words.

---

## Architecture

| Requirement | Component | File |
| :--- | :--- | :--- |
| **No wake word, respond only when needed** | Silero VAD + MLX Whisper + `JarvisIntentGate` | [`src/jarvis/voice_gates.py`](src/jarvis/voice_gates.py) |
| **"Shut up" / "Be quiet" -> Beep Mode** | `JarvisState` (`VOICE` vs `BEEP_ONLY` + sine PCM beep synth) | [`src/jarvis/state.py`](src/jarvis/state.py) |
| **"You can talk" -> Unmute & Digest** | `JarvisState.set_unmute()` | [`src/jarvis/state.py`](src/jarvis/state.py) |
| **Hear only speech meant for Jarvis** | `ExecutiveFilter` (decision model) | [`src/jarvis/executive_filter.py`](src/jarvis/executive_filter.py) |
| **Drive Claude Code, OpenCode, Hermes** | `ClaudeCodeAdapter`, `OpenCodeSSEAdapter`, `HermesAdapter` | [`src/jarvis/agent_watchers.py`](src/jarvis/agent_watchers.py) |
| **Drive T3 Code** | `T3Adapter` | [`src/jarvis/t3_adapter.py`](src/jarvis/t3_adapter.py) |

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
  thread there that first syncs with master. Coding with no project named runs in `cwd` from `agents.json`. Threads
  use your T3 default model and mode; Jarvis watches only its newest thread.

### 4. Memory
Jarvis remembers facts about you between runs (local [Mem0](https://github.com/mem0ai/mem0) with Qdrant in
`~/.jarvis/memory`). It needs the Ollama app running with the embedding model: `ollama pull bge-m3`. Without Ollama,
Jarvis still runs, just without memory.

### 5. Run Jarvis
On laptop speakers Jarvis ignores the mic while it talks (`MUTE_MIC_WHILE_SPEAKING=true`), so it doesn't hear itself; you can't interrupt it mid-sentence then. With headphones, set it to `false` to cut Jarvis off with "shut up".
```bash
uv run jarvis
```

### Voice Commands
- **"Code [task]..." / "Build [feature]..." / "Fix [bug]..."** — Sends the task to your default coding agent.
- **"Check my calendar" / "Email Minh..." / "Look up..."** — Sends it to Hermes.
- **Answering an agent** ("yes", "use Postgres") — Goes to the question or approval the agent is waiting on.
- **"Stop Claude"** / **"Stop that"** — Stops the agent's current turn.
- **"Shut up"** / **"Be quiet"** — Immediately stops TTS playback and switches Jarvis into `BEEP_ONLY` mode. When a coding agent hits a blocker or needs a decision, Jarvis plays a short chime and queues the topic silently.
- **"You can talk"** — Switches Jarvis back to `VOICE` mode and immediately reads a concise, jargon-free digest of everything queued while muted.
