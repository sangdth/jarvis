# Jarvis — Always-On Voice Executive Harness

Jarvis is an always-on voice harness built on **Pipecat**, **Picovoice Eagle**, **Faster-Whisper**, and **ACP / Claude Code Streams**.

It listens continuously without a wake word, locks onto **your voice only**, watches your coding agents (**Claude Code**, **OpenCode**, **Pi**) in the background, strips out technical jargon, and surfaces only high-level decisions.

---

## Architecture

| Requirement | Component | File |
| :--- | :--- | :--- |
| **Talk to me only** | `SpeakerVerificationGate` (Picovoice Eagle voiceprint) | [`src/jarvis/voice_gates.py`](src/jarvis/voice_gates.py) |
| **No wake word, respond only when needed** | Silero VAD + Faster-Whisper + `JarvisIntentGate` | [`src/jarvis/voice_gates.py`](src/jarvis/voice_gates.py) |
| **"Shut up" / "Be quiet" -> Beep Mode** | `JarvisState` (`VOICE` vs `BEEP_ONLY` + sine PCM beep synth) | [`src/jarvis/state.py`](src/jarvis/state.py) |
| **"You can talk" -> Unmute & Digest** | `JarvisState.set_unmute()` | [`src/jarvis/state.py`](src/jarvis/state.py) |
| **Strip technical jargon & routine logs** | `ExecutiveFilter` | [`src/jarvis/executive_filter.py`](src/jarvis/executive_filter.py) |
| **Watch Claude Code, OpenCode, Pi** | `ClaudeCodeStreamAdapter`, `OpenCodeSSEAdapter`, `ACPStdioAdapter` | [`src/jarvis/agent_watchers.py`](src/jarvis/agent_watchers.py) |

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
- `ANTHROPIC_API_KEY`: Used by the Executive Filter and Jarvis's conversational brain.
- `PICOVOICE_ACCESS_KEY`: Free key from [Picovoice Console](https://console.picovoice.ai/) for speaker verification.
- `ELEVENLABS_API_KEY`: For ultra-low-latency voice output.

### 3. Enroll Your Voice ("Talk to me only")
Record a 10-second voiceprint so Jarvis ignores everyone else in the room:
```bash
jarvis-enroll
```

### 4. Configure Watched Coding Agents
Edit [`agents.json`](agents.json) to point to your active **Claude Code**, **OpenCode** (`opencode serve`), and **Pi** (`pi --acp`) workspaces.

### 5. Run Jarvis
```bash
jarvis
```

### Voice Commands
- **"Shut up"** / **"Be quiet"** — Immediately stops TTS playback and switches Jarvis into `BEEP_ONLY` mode. When a coding agent hits a blocker or needs a decision, Jarvis plays a short chime and queues the topic silently.
- **"You can talk"** — Switches Jarvis back to `VOICE` mode and immediately reads a concise, jargon-free digest of everything queued while muted.
