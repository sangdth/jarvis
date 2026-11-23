# brain

Always-on goal and problem reconciler, running 24/7 in one process with (or next to) the voice harness. It holds a
board of goals and problems, runs `pi` workers in isolated worktrees, verifies their work, and pushes a branch — it
never merges to `master` on its own. The voice asks it for everything that is not coding; the cockpit shows it live.

It needs no setup of its own: `jarvis` runs it with the voice in one process on this checkout, and both share
`JARVIS_HOME/.env`, `SOUL.md` (its `projects_dir` decides where the brain may work) and the token in
`JARVIS_HOME/brain_token`. Its HTTP/SSE/WebSocket gateway is the only network surface Jarvis exposes.

## Quick start

```bash
jarvis                      # the voice and the brain together, in this terminal
jarvis start                # 24/7: launchd keeps this checkout running, and how you restart after an edit
jarvis status               # version, uptime, mode, spend today, tasks, voice
jarvis top                  # the cockpit (cargo build --release --workspace first)
```

From the voice: "add a goal for ...", "what happened overnight", "switch to autonomous mode", "morning brief".

From the iPhone (Tailscale), an iOS Shortcut can `POST /v1/ask` with `{"input": "..."}` and speak the returned
`spoken` field. The token is whatever `BRAIN_API_KEY` is set to in `.env`, or the contents of
`JARVIS_HOME/brain_token`; `GET /health` needs none.

## `.env` keys it reads

`ALLOWED_REPOS`, `DAILY_BUDGET_USD`, `DEFAULT_MODE` (`ASSIST` or `AUTONOMOUS`), `BRAIN_HOST`,
`BRAIN_PORT`, `PI_BIN`, `MAX_RUN_SECONDS`, `VERIFY_TIMEOUT_SECONDS`, `SANDBOX_ENABLED`, `MAX_PERSONAL_SKILLS`,
`AUTO_RESEARCH_AFTER_STRIKES`, `SELF_UPGRADE_VERIFY_CMD`, `PROJECTS_DIR`. See `src/jarvis/defaults/env.example`.

## Layout

- `src/brain/server.py` — the one HTTP/SSE/WS gateway on `:8643`: the brain's API, telemetry, `/health`,
  `/v1/status`, `/v1/board`, `/v1/tasks`, `/v1/mode`, `/v1/restart`, and the thin client's routes.
- `src/brain/reconciler.py` — claims tasks, runs `pi` workers, verifies, pushes, self-upgrades.
- `src/brain/db.py` + `schema.sql` — the board (SQLite WAL, one writer).
- `src/brain/skills.py`, `sandbox.py`, `telemetry.py`, `upgrade.py`, `cli.py`.
- `defaults/pi/board.ts` — the `pi` extension workers use (task board, notes, approvals, write guard).
- `tests/` — the suite (`uv run pytest apps/brain/tests`).

Rules and limits: [`AGENTS.md`](AGENTS.md). Why the choices: `docs/DECISIONS.md`.
