# Superbrain

Always-on goal and problem reconciler daemon running 24/7 on the **Mac mini**, controlled by **Jarvis** from your
**MacBook** (`HermesAdapter`) or **iPhone** (`POST /v1/ask` via iOS Shortcuts over Tailscale).

## Quick Start (Mac mini)

```bash
cd ~/Projects/superbrain
uv sync
mkdir -p ~/.config/superbrain
chmod 700 ~/.config/superbrain
cat > ~/.config/superbrain/.env << 'EOF'
SUPERBRAIN_API_KEY="replace-with-random-secret"
ALLOWED_REPOS="jarvis"
DAILY_BUDGET_USD=200.0
EOF
chmod 600 ~/.config/superbrain/.env

# Expose over Tailscale HTTPS (Tailnet only)
tailscale serve --bg --https=8643 http://127.0.0.1:8643

# Run daemon or install launchd agent
uv run superbrain serve
uv run superbrain launchd --install
```

## Updating the daemon

The launchd agent (`com.sangdth.superbrain`, `KeepAlive=true`) runs whatever commit the checkout at
`~/Projects/superbrain` has, so merged PRs only take effect after that checkout moves. One command does the safe
fast-forward, dependency sync, and restart:

```bash
uv run superbrain upgrade --check   # read-only: HEAD vs origin/master, ahead/behind, dirty tree, dep changes
uv run superbrain upgrade           # fetch -> ff-only merge -> uv sync only if deps changed -> restart
```

Behaviour:

- Records `system_state.last_good_sha` **before** changing anything, so a failed startup (or 2 consecutive
  post-upgrade task failures) can roll back automatically.
- Refuses on a dirty tree; pass `--stash` to auto-stash with a `superbrain-upgrade-<timestamp>` message
  (recover with `git stash list` / `git stash pop`).
- Refuses on divergence (local HEAD and `origin/master` both have unique commits) with a backup/rebase remedy.
  It never forces, merges, or resets in that case.
- Runs `uv sync` only when `pyproject.toml` or `uv.lock` changed between the old and new HEAD.
- `--no-restart` stops after the checkout update. `--check --fetch` refreshes `origin/master` for an accurate
  read-only report (the checkout is still untouched).
- Restart semantics: on macOS, when the LaunchAgent is loaded, it runs
  `launchctl kickstart -k gui/<uid>/com.sangdth.superbrain`; otherwise it prints the manual restart instruction.
  A clean daemon exit is enough because `KeepAlive` respawns it.

`superbrain upgrade` is for the checkout (owner-driven). Autonomous `repo='superbrain'` tasks use the same
divergence rule: the builder worktree is based on `origin/master` unless local `HEAD` is genuinely ahead, a
diverged checkout is refused (blocker outbox item), and a rejected `git push origin HEAD:master` is reported
instead of being ignored.

## Connect MacBook (`jarvis`)

In `~/.config/jarvis/agents.json` on your MacBook, point the `hermes` entry to your Mac mini's Tailscale URL:

```json
{
  "id": "hermes",
  "name": "Hermes",
  "type": "hermes",
  "url": "https://<mac-mini>.<tailnet>.ts.net:8643",
  "role": "everything that is not coding: research, web lookups, notes, email, calendar, reminders, files, goals, morning brief, night mode",
  "enabled": true
}
```

And set `HERMES_API_KEY` in `~/.config/jarvis/.env` on your MacBook to match `SUPERBRAIN_API_KEY`.

## Connect iPhone (Action Button / Siri Shortcut)

Create an iOS Shortcut named **Ask Jarvis**:
1. **Dictate Text**
2. **Get Contents of URL**: `https://<mac-mini>.<tailnet>.ts.net:8643/v1/ask`
   - Method: `POST`
   - Headers: `Authorization: Bearer <SUPERBRAIN_API_KEY>`, `Content-Type: application/json`
   - Request Body (JSON): `{"input": "<Dictated Text>"}`
3. **Get Dictionary Value** for `spoken` -> **Speak Text**

## Fullscreen dashboard (`superbrain top`)

`tui/` is a Rust + Ratatui cockpit that shows the whole brain at once: a live architecture schematic,
the streaming `pi` prompt/tool transcript, the task DAG with metric convergence, and the durable ledger
(attempts, skills, outbox, notes, DB health + logs).

```bash
cd tui && cargo build --release && cd ..   # once
uv run superbrain top                      # fullscreen cockpit
```

It reads `state.db` **read-only** (so it can never block the daemon's WAL writers) and subscribes to
`GET /v1/telemetry/events` for live phase/RPC/progress events. Operator keys: `a`/`r` approve or reject a
queued outbox approval, `m` toggles `ASSIST`/`AUTONOMOUS`, `x` stops the active run, `:` sends a
natural-language instruction, `?` shows all keybindings. See [`tui/README.md`](tui/README.md).
