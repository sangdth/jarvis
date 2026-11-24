# Contracts between the apps

Each contract lives in exactly one place, and every app that reads it points here. Change a contract in the
owning app, then update the clients in the same commit.

| Contract | Canonical file | Written by | Read by |
| :--- | :--- | :--- | :--- |
| The gateway's HTTP/SSE/WS API | `schemas/openapi.json` | `apps/brain/src/brain/server.py` (+ `jarvis_voice/remote_audio.py` routes) | `apps/voice` (`brain_client.py`), `apps/dashboard` (`http.rs`), `jarvis` (`cli.py`), the thin client, iOS Shortcuts, a future React/Go client |
| The brain's SQLite board | `apps/brain/src/brain/schema.sql` | `apps/brain/src/brain/db.py` (WAL, the only writer) | `apps/dashboard/src/db.rs` (read-only, its own connection) |
| Agent roster | `src/jarvis/defaults/agents.json` | the user (`JARVIS_HOME/agents.json`) | `apps/voice` (`orchestrator.py`, `prompts.py`) |
| Settings | `src/jarvis/defaults/env.example` | the user (`JARVIS_HOME/.env`) | all three apps, through `jarvis_core.settings.EnvSettings` |

`schema.sql` stays inside the Python package because the daemon reads it as package data at startup. The
dashboard never reads it: it queries the tables it draws, so a column added there is additive and safe.

A contract test in `apps/brain/tests` covers the HTTP surface; the dashboard's parser tests cover its side.
