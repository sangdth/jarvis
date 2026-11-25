# Hex Notes: Comparison, Adopted Ideas, and Parked Ideas

Reviewed 2026-11-08 against Hex (`anomalyco/hex`, `~/Projects/others/hex`) to decide which ideas fit Jarvis's thin
voice-harness model.

## 1. Hex vs. Jarvis

| Dimension    | Hex (`anomalyco/hex`)                      | Jarvis (`sangdth/jarvis`)                          |
| :----------- | :----------------------------------------- | :------------------------------------------------- |
| Paradigm     | Offline voice dictation & meeting recorder | Always-on voice harness + background brain daemon  |
| Platforms    | macOS, Linux, Windows (Rust/C++/Swift)     | macOS Apple Silicon (Python, Rust dashboard, Swift)|
| Activation   | Physical hotkey (hold/toggle/double-tap)   | Ambient no-wake-word + Silero VAD + Smart Turn v3  |
| Identity     | None (transcribes any speaker)             | Voiceprint (`sherpa-onnx` ERes2Net) + speaker ID   |
| Speech check | Implicit (hotkey pressed)                  | Laya `jarvis-intent` + OpenRouter fallback         |
| STT          | Streaming GGUF (`transcribe.cpp`)          | Segment MLX Whisper (`large-v3-turbo` / PhoWhisper)|
| Output       | Silent (HUD + paste into focused app)      | ElevenLabs TTS + `AVAudioEngine` echo cancellation |
| Action layer | Clipboard/AX paste, read-only `hex mcp`    | Brain orchestrator -> T3 Code, `pi`, Claude, OC    |

## 2. Adopted (Built)

- **OpenCode 2 `service.json` discovery & `cwd` scoping** (`apps/voice/src/jarvis_voice/opencode_adapter.py`): Reads
  `$XDG_STATE_HOME/opencode/service.json` (verifying owner-only `0600`, no symlink, loopback URL, live pid) unless
  `OPENCODE_SERVER_PASSWORD` or a custom `url` pins a server, and scopes listings, new sessions, and events to `cwd`
  so Jarvis never approves tool calls in another project. (`apps/brain/src/brain/runners/opencode.py` instead runs
  one-shot `opencode run --format json` subprocesses without an SSE stream.)
- **Audio segment retention** (`SAVE_SEGMENTS=N` in `voice_gates.py`): Writes 16 kHz WAVs to `JARVIS_HOME/clips/`
  (`0700`/`0600`, rotated to newest `N`) for every segment Whisper receives — including Whisper's no-text drops — so
  real misses can be re-scored offline.

## 3. Rejected (With Evidence)

- **Whisper `initial_prompt` vocabulary:** Tested 2026-11-06 (`docs/research/voice-recognition-misses.md`, finding 6);
  leaked prompt words into unrelated speech and fixed no project names (`docs/DECISIONS.md`).
- **Pre-Whisper duration / DC-offset gate:** Redundant with Silero VAD (`start_secs=0.2`), MLX Whisper's
  `no_speech_prob` / compression filters, `SpeakerEmbedder` readiness checks, and `_WHISPER_FILLERS`.
- **`utterances.jsonl` flight recorder:** Superseded by terminal logs and `apps/brain/src/brain/traces.py`.
- **`hex mcp` meeting bridge:** Belongs in an agent's MCP config if Hex is ever used, not in Jarvis.
- **Streaming STT during capture & QuickJS transforms:** Speaker verification needs the full sentence anyway, and
  agents own transforms/logic.

## 4. Parked (Build Only on a Trigger)

- **WAV replay through the live pipeline (`AUDIO_FIXTURE`):** Feed a recorded WAV in 20 ms blocks into
  `MacVoiceInputTransport` without opening the hardware mic (`AVAudioEngine` still wakes the mic unless the output
  engine path is separated). Park until offline Whisper + speaker rescoring fails to explain a real miss.
- **Foreground app and window title context:** Reading `AXFocusedWindow` needs Accessibility permission and can
  expose sensitive titles (email subjects); the newest-repo rule already handles "where I left off." Revisit only if
  real logs show frequent "here" / "this project" failures.
