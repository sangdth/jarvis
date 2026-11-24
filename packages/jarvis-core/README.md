# jarvis-core

What every app in this monorepo shares: `JARVIS_HOME` (one state home for the voice harness, the brain and the
dashboard), the `.env` settings base, the shared brain token, and `SOUL.md` (personality and `projects_dir`).

Imported by `jarvis-voice`, `brain` and the root `jarvis` CLI. Keep it dependency-light: `pydantic-settings`
and the standard library, nothing else.
