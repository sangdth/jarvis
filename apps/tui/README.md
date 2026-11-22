# jarvis-top — the cockpit (Ratatui)

A single-binary, fullscreen terminal dashboard for Jarvis's brain (`apps/brain`). Designed to run 24/7 on a
machine inside Ghostty (or any truecolor terminal) and to be readable over Tailscale SSH from a laptop or phone.
It is one of the three apps in this repository; it never writes state, only reads it and calls the brain's API.

```
┌ header ──────────────────────────────────────────────────────────────────────┐
│ BRAIN [AUTONOMOUS] 127.0.0.1:8643 ● live  phase pi_running · SG-4.2       │
│ budget ████░░░░░░░░░░░░░░░░░░░░░░ $15.76 / $200.00 (8%)  [! 1 APPROVAL]        │
├───────────────────────────────┬──────────────────────────────────────────────┤
│ 1 ARCHITECTURE (live schema)  │ 2 PROMPTS (live pi JSONL stream)              │
├───────────────────────────────┤                                               │
│ 3 TASKS (DAG + metrics)       │                                               │
├───────────────────────────────┴──────────────────────────────────────────────┤
│ 4 LEDGER · Attempts │ Skills │ Outbox │ Notes │ DB & Logs                    │
└──────────────────────────────────────────────────────────────────────────────┘
 q quit  ? help  Tab/1-4 focus  z zoom  a/r approve  m mode  x stop  : ask  y copy
```

## Build & run

```bash
cargo build --release --workspace   # ~2.7 MB binary, no runtime deps; target/release/jarvis-top
jarvis top                          # finds it on PATH, or in target/{release,debug}
```

An installer or a teammate gets it with `cargo build --release --workspace` in the checkout; there is no packaged
binary to fetch, because the checkout is the runtime.

For an always-on screen, run it inside Ghostty with a Nerd Font, e.g.:

```bash
# Ghostty config (Mac mini)
font-family = JetBrainsMono Nerd Font
font-size = 13
window-decoration = false
macos-non-native-fullscreen = true
```

## Data planes

| Plane | Source | Why |
| :--- | :--- | :--- |
| Durable | `state.db` opened `SQLITE_OPEN_READ_ONLY`, polled every 700 ms | Never blocks the daemon's WAL writers; survives daemon restarts |
| Durable | `sessions/**/*.jsonl` tailed by byte offset | Full prompt / tool-call / result transcripts |
| Live | `GET /v1/telemetry/events` (SSE, auto-reconnect) | Phase transitions, `board.ts` RPC calls, streaming `pi` cost |

The dashboard never writes to `state.db`. Operator actions go through the authenticated loopback API
(`/v1/ask`, `/v1/outbox/{id}/resolve`, `/v1/runs/{id}/stop`).

## Keys

| Key | Action |
| :--- | :--- |
| `q` / `Ctrl-c` | quit |
| `?` | keybinding overlay |
| `Tab`, `1`–`4` | focus Architecture / Prompts / Tasks / Ledger |
| `z` | zoom the focused pane to the full body |
| `j`/`k`, `g`/`G`, `PgUp`/`PgDn` | move selection / scroll |
| `[` / `]` | switch ledger tabs |
| `f` | toggle live follow on the prompt stream |
| `Enter` | inspect node · expand prompt · open skill/task detail · load an attempt's transcript |
| `a` / `r` | approve / reject the selected queued outbox approval |
| `m` | toggle `ASSIST` ↔ `AUTONOMOUS` |
| `x` | stop the active run |
| `:` | send a natural-language instruction to `/v1/ask` |
| `y` | copy the focused content to the macOS clipboard |

## Diagnostics

```bash
jarvis-top --render-check   # paint one frame into a TestBackend and print it (layout smoke test)
jarvis-top --selftest 4     # run the real workers for 4s and print what arrived
```

`--render-check` honours `SB_DEMO_PANE`, `SB_DEMO_TAB`, `SB_DEMO_MODAL`, and `SB_DEMO_ZOOM` so every
pane and modal can be rendered headlessly.

## Layout of the source

| File | Responsibility |
| :--- | :--- |
| `src/main.rs` | terminal setup, worker threads, event loop, `--render-check` / `--selftest` |
| `src/config.rs` | `JARVIS_HOME/brain` (or `$BRAIN_HOME`) + the shared `.env` / `brain_token` |
| `src/db.rs` | read-only SQLite snapshot loader |
| `src/http.rs` | loopback HTTP client + chunked-aware SSE reader |
| `src/session.rs` | incremental JSONL tailer and `pi` turn parser |
| `src/app.rs` | state, event application, key handling, operator actions |
| `src/ui/` | header/footer/modals, topology canvas, prompts, tasks, ledger |

## Tests

```bash
cargo test --workspace     # unit tests: config, model, session parser, HTTP client, tree flattening
cargo clippy --workspace --all-targets -- -D warnings
```
