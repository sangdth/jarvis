# Jarvis — Always-On Voice Executive Harness

Jarvis is an always-on voice harness built on **Pipecat**, **MLX Whisper**, the **Claude Agent SDK** and the **OpenCode v2 API**.

It listens continuously without a wake word, watches your coding agents (**Claude Code**, **OpenCode**) in the background, strips out technical jargon, and surfaces only high-level decisions.

---

## Architecture

| Requirement | Component | File |
| :--- | :--- | :--- |
| **No wake word, respond only when needed** | Silero VAD + MLX Whisper + `JarvisIntentGate` | [`src/jarvis/voice_gates.py`](src/jarvis/voice_gates.py) |
| **"Shut up" / "Be quiet" -> Beep Mode** | `JarvisState` (`VOICE` vs `BEEP_ONLY` + sine PCM beep synth) | [`src/jarvis/state.py`](src/jarvis/state.py) |
| **"You can talk" -> Unmute & Digest** | `JarvisState.set_unmute()` | [`src/jarvis/state.py`](src/jarvis/state.py) |
| **Strip technical jargon & routine logs** | `ExecutiveFilter` | [`src/jarvis/executive_filter.py`](src/jarvis/executive_filter.py) |
| **Watch Claude Code, OpenCode** | `ClaudeCodeAdapter`, `OpenCodeSSEAdapter` | [`src/jarvis/agent_watchers.py`](src/jarvis/agent_watchers.py) |

---

## Quick Start

### 1. Install Dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Configure `.env`
```bash
cp .env.example .env
```
Fill in:
- `OPENROUTER_API_KEY`: used for Jarvis's conversational brain (`EXECUTIVE_MODEL`) and the agent-event filter (`FILTER_MODEL`), both `deepseek/deepseek-v4.1-flash` with thinking off, and for the decision model that tells whether a sentence is meant for Jarvis (`CLASSIFIER_MODEL`, `typesafe/jev-1.14`). Left empty, the filter runs on offline keyword heuristics.
- `DEFAULT_CODING_AGENT`: the exact `agents.json` id (e.g. `opencode`) that gets tasks like *"build a login screen"* when you don't name an agent. Empty means the first enabled agent. An unknown id stops Jarvis at startup.
- `ELEVENLABS_API_KEY`: For ultra-low-latency voice output.

### 3. Configure Watched Coding Agents
Edit [`agents.json`](agents.json): set each agent's `cwd` to the project it should work in.
- **Claude Code** runs through the Agent SDK with `acceptEdits`: file edits go through without asking. Other tools your
  Claude settings don't allow, and Claude's own questions, are read out, and Claude waits for your answer.
- **OpenCode** needs its v2 server running with the same password Jarvis has in `.env`:
  `OPENCODE_SERVER_PASSWORD=<same value> opencode serve --port 4096`, started in the project folder.

### 4. Run Jarvis
On laptop speakers Jarvis ignores the mic while it talks (`MUTE_MIC_WHILE_SPEAKING=true`), so it doesn't hear itself; you can't interrupt it mid-sentence then. With headphones, set it to `false` to cut Jarvis off with "shut up".
```bash
uv run jarvis
```

### Voice Commands
- **"Code [task]..." / "Build [feature]..." / "Fix [bug]..."** — Sends the task to your default coding agent.
- **Answering an agent** ("yes", "use Postgres") — Goes to the question or approval the agent is waiting on.
- **"Stop Claude"** / **"Stop that"** — Stops the agent's current turn.
- **"Shut up"** / **"Be quiet"** — Immediately stops TTS playback and switches Jarvis into `BEEP_ONLY` mode. When a coding agent hits a blocker or needs a decision, Jarvis plays a short chime and queues the topic silently.
- **"You can talk"** — Switches Jarvis back to `VOICE` mode and immediately reads a concise, jargon-free digest of everything queued while muted.
