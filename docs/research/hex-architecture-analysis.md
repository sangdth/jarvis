# Hex vs. Jarvis Architecture Analysis

This document records the architectural comparison between **Hex** (`~/Projects/others/hex`), a native systems-level voice dictation and meeting recording appliance built by Anomaly, and **Jarvis** (`sangdth/jarvis`), an always-on ambient voice executive harness for autonomous agent orchestration.

---

## 1. Executive Summary

| Dimension | Hex (`anomalyco/hex`) | Jarvis (`sangdth/jarvis`) |
| :--- | :--- | :--- |
| **Core Paradigm** | Voice-to-text input appliance, meeting recorder & screen annotator | Always-on, full-duplex conversational voice executive harness for AI agents |
| **Language & Scale** | ~250,200 LOC (Rust, C++, Swift, WGSL, TS) | ~3,320 LOC (Python on Pipecat) |
| **OS Platforms** | macOS (`arm64`), Linux (`x86_64`/`arm64` X11/Wayland), Windows (`x64`/`arm64`) | macOS (Apple Silicon `arm64` only) |
| **Activation Trigger** | Physical shortcut (Hold/Toggle/Double-tap, Caps Lock, Mouse) | Ambient no-wake-word continuous listening |
| **Identity & Security**| None (transcribes whoever speaks into the mic) | Voiceprint verification (`sherpa-onnx` 3D-Speaker ERes2Net) |
| **Intent Recognition** | Implicit (user pressed hotkey) | Neural decision model (`typesafe/jev-1.14` on OpenRouter) |
| **Speech-to-Text (STT)**| `transcribe.cpp` (Metal/Vulkan GGUF, 17+ models) + Apple `SpeechAnalyzer` | `mlx_whisper` (`whisper-large-v3-turbo` / `phowhisper-large-vien-cs`) |
| **Decoding Latency** | Streaming during capture (decodes *while* speaking) | Batch transcription per VAD sentence (after 0.6s pause) |
| **Voice Output (TTS)** | None (silent; visual WGSL dot-scope HUD + audio chimes) | Full-duplex ElevenLabs `eleven_v4_turbo` (Text-to-Dialogue + emotions) |
| **Echo / Bleed Handling** | Post-hoc dual-track meeting bleed classifier (`src/meeting/echo.rs`; no TTS output) | Real-time `AVAudioEngine` Voice Processing (`mac_audio.py`) for speaker barge-in |
| **Downstream Target** | Pastes text into focused app via clipboard/AX; MCP query | Dispatches tasks to T3 Code, Claude Code, OpenCode, Hermes, Pi |
| **OpenCode Role** | Stateless generation (`POST /api/experimental/generate`) for rewrites | Stateful SSE + session supervisor for coding runs & approvals |
| **Memory / State** | Chronological SQLite/files + in-memory BM25 meeting search | Semantic memory (Mem0 + Qdrant + Ollama `bge-m3`) |
| **Cost / Connectivity**| 100% offline ($0 cloud cost) | Hybrid (Local STT/VAD/embeddings + cloud OpenRouter & ElevenLabs) |

---

## 2. Hex Architecture Breakdown

Hex operates as a monolithic, single-process desktop daemon (`src/main.rs`) hosting:

1. **Audio Engine & Timeline (`src/audio/`, `src/dictation/audio.rs`):**
   - Built on `cpal` with CoreAudio, PipeWire, and WASAPI listeners.
   - Authoritative timeline with warm pre-roll ring buffering so initial speech before keypress registration is not dropped.
   - Dual-track capture: microphone + system audio loopback (CoreAudio process tap / ScreenCaptureKit on macOS, PipeWire monitor on Linux, WASAPI loopback on Windows).
   - Signal-level echo classification (`src/meeting/echo.rs`): computes 20ms frame energy envelopes over a lag search window (`-15` to `+30` frames) and models reverberation decay (`1.5 dB/frame`) to categorize segments as remote speaker `Bleed` vs. `Local` speech without destructive filtering.
   - Deterministic test harness (`src/audio/fixture.rs`): `HEX_AUDIO_FIXTURE=<wav>` plays audio in 10ms real-time chunks stamped with `CaptureInstant::now()`, bypassing hardware mic enumeration for benchmarks and CI.

2. **Inference Pipeline (`src/transcription/`):**
   - Single-thread `TranscriptionEngine` leases Metal/Vulkan compute via patched `transcribe.cpp`.
   - Streams audio chunks during capture; releasing the key finishes within a `1,500ms` deadline with offline fallback.
   - Background loaded Silero VAD (`src/transcription/speech_detector.rs`, threshold `0.4`, min speech `160ms`) keeps silence and clicks away from GPU models.
   - Zero-copy Hugging Face cache discovery (`src/transcription/found_models.rs`): scans `~/.cache/huggingface/hub` headers and adopts existing GGUF model blobs.
   - Native macOS `SpeechAnalyzer` bridge (`src/apple_speech.swift`).

3. **Context & Personal Transformations (`src/dictation/context.rs`, `src/personal_commands.rs`):**
   - Captures active application bundle, window title, and browser URL (AppleScript dictionaries for Safari/Chromium, AXWebArea `AXURL`).
   - Executes user TypeScript configs (`~/.config/hex/hex.config.ts`) via embedded QuickJS (`rquickjs`) with runtime memory caps, per-call deadlines, and `oxc_parser` JS stripping.

4. **UI & MCP Layer (`src/rune_ui/`, `src/meeting/mcp.rs`):**
   - Custom cross-platform GPU UI (**Rune**) with WGSL dot-scope shaders (`rune_hud.wgsl`).
   - Serves an embedded MCP server (`hex mcp` via `rmcp`) exposing meeting notes and BM25 transcript search.

---

## 3. How Hex is Better Than Jarvis

1. **Zero Cloud Dependencies & True Local Privacy:** Runs 100% offline. STT, Apple Intelligence summarization, and QuickJS transforms require no external APIs or subscription keys.
2. **Streaming-During-Capture & Model Variety:** Audio is decoded on Metal/Vulkan *while* speaking, resulting in lower perceived latency. Supports 17+ optimized speech models (including Parakeet v3/Ultra, SenseVoice, Moonshine) with vocabulary biasing.
3. **Cross-Platform OS Integration:** Operates across macOS, Linux, and Windows with global hotkey suppression, active keyboard layout mapping (TIS, XKB), and generation-safe clipboard restoration.
4. **Dual-Track Meeting & Screen Capture:** Records system audio and microphone with acoustic bleed detection, generates live meeting chapters, and embeds video/annotation recording (`scrawl-capture`).
5. **Deterministic Pipeline Testing:** `HEX_AUDIO_FIXTURE` enables headless testing of the full audio transport and VAD without requiring a microphone.

---

## 4. How Jarvis is Better Than Hex

1. **Ambient Hands-Free Autonomy (No Physical Hotkey):** Hex requires holding or toggling physical keys. Jarvis is always on in the room with no wake words and no push-to-talk.
2. **Acoustic & Biometric Security:** Anyone can use Hex's hotkey. Jarvis enrolls the owner's voiceprint (`sherpa-onnx` 3D-Speaker) and verifies every sentence in parallel with Whisper (0ms added delay), rejecting colleagues, room talk, and TV noise.
3. **Conversational Intent Gating:** Uses a neural decision model (`typesafe/jev-1.14`) with a 20s context window to distinguish self-talk from instructions addressed to Jarvis.
4. **Executive Agent Routing:** Hex only pastes text. Jarvis controls real agents: opens Git repos by spoken name, starts/attaches T3 Code threads, drives Claude Code, OpenCode, and Hermes Agent, reads out questions, and retells multi-page markdown results in ~10 plain words.
5. **Approval Safety Invariant:** Agent questions and approval requests bypass the LLM completely, preventing autonomous hallucinations of destructive commands.
6. **Full-Duplex Speech & Emotion:** Synthesizes voice via ElevenLabs `eleven_v4_turbo` with emotional tags, supported by hardware echo cancellation (`AVAudioEngine`) for natural barge-in interruptions.
7. **Semantic Memory:** Extracts cross-session user facts via local Mem0 + Qdrant + Ollama `bge-m3`.
8. **Architectural Agility:** 3,324 lines of Python vs. 250,200 lines of Rust, making new agent protocol adapters fast to implement and maintain.
