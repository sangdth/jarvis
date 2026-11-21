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
DAILY_BUDGET_USD=20.0
EOF
chmod 600 ~/.config/superbrain/.env

# Expose over Tailscale HTTPS (Tailnet only)
tailscale serve --bg --https=8643 http://127.0.0.1:8643

# Run daemon or install launchd agent
uv run superbrain serve
uv run superbrain launchd --install
```

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
