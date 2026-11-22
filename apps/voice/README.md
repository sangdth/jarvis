# jarvis-voice (the voice layer)

Jarvis's always-on ear and mouth: Pipecat pipeline, local MLX Whisper, the speech check (`JarvisIntentGate`,
Laya first with `FILTER_MODEL` as the fallback), the executive model, ElevenLabs voice, Mem0 memory, screen
looks, and the agent watchers (T3 Code, Claude Code, OpenCode, Pi) that hand work to each agent's own harness.

Started by the root `jarvis` command, which also starts the brain. Architecture: `docs/architecture.md`.
