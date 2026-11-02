# Jarvis

An always-on voice harness (Pipecat) that watches coding agents (Claude Code, OpenCode) and talks to the user.
No wake word. Architecture: `docs/architecture.md`. Setup and voice commands: `README.md`.

## Commands

- Run: `uv run jarvis` (needs `.env`; OpenCode needs `opencode serve --port 4096` with the same password)
- Test: `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`
- Speech benchmark: `uv run python scripts/bench_intent.py` (paid OpenRouter calls, about 100 at 3 rounds)

## Where to fix "Jarvis"

Start from what the user heard or didn't. The obvious file is often wrong: "Jarvis ignores me" is usually the
speech check dropping the sentence, not the conversation model.

| Symptom                              | Where                                                  |
| :----------------------------------- | :----------------------------------------------------- |
| Doesn't react to something said      | `executive_filter.py` `INTENT_QUESTION`                |
| Mute / unmute phrase missed          | `state.py` `_MUTE_PATTERNS`, `_UNMUTE_PATTERNS`        |
| Reacts, but the reply is wrong       | `main.py` `JARVIS_EXECUTIVE_SYSTEM_PROMPT`             |
| Says something wrong about an agent  | `agent_watchers.py` `_surface` call sites              |
| Retold agent turn is long or odd     | `main.py` `AGENT_REPORT_PROMPT`, `state.py` `_retell`  |
| Wrong agent gets a task or answer    | `agent_watchers.py` `AgentOrchestrator.find`           |
| Beeps, muted queue, unmute digest    | `state.py` `JarvisState`                               |
| Mic, Whisper, voice, interruptions   | `main.py` pipeline, `voice_gates.py`                   |
| Model names, keys                    | `config.py`, `.env`, `.env.example`                    |

Debug first, then edit: read the app's terminal log (T3 Code keeps it in `~/.t3/userdata/logs/terminals/*.log`).
`[User Transcript]` shows what Whisper heard; the lines after it show whether the sentence was passed on or ignored.

## Rules

- **Speech check changes need the benchmark.** Any edit to `INTENT_QUESTION` or `FILTER_MODEL` runs
  `scripts/bench_intent.py` before and after and quotes both results. It exits 1 below 97%; going below needs
  Sang's OK. New misses found in real use become new cases in that script.
- **Approvals and questions never go through an LLM.** Agent questions and approval requests are spoken as they
  are; only finished turns (`AgentTopic.is_report`) go to the conversation model, as a system message that calls
  no tool. This keeps the model from approving anything on its own.
- **Watcher strings start with the verb** (`asks: ...`, `needs approval: ...`); `JarvisState` adds the agent name.
- **Agent ids are exact** (`claude`, `opencode`), matched as written in `agents.json`. A bad
  `DEFAULT_CODING_AGENT` stops Jarvis at startup.
- **Two model settings, one job each:** `EXECUTIVE_MODEL` talks and retells turns; `FILTER_MODEL` (a decision
  model on `/api/alpha/decisions`) decides whether a sentence is meant for Jarvis.
- Mock HTTP and the Claude SDK in unit tests. Live checks against real agents go in a throwaway script, not in `tests/`.

## Later, not now

- More agents (Pi, Codex): add an adapter with the same shape as `ClaudeCodeAdapter` / `OpenCodeSSEAdapter`.
- Speaker verification: use sherpa-onnx, not Picovoice.
- Laya as a local speech check: parked; it lost to the OpenRouter decision models.
