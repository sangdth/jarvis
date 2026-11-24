# jarvis-dashboard — the dashboard (Ratatui)

A single-binary, fullscreen terminal dashboard for Jarvis's brain (`apps/brain`). Designed to run 24/7 on a
machine inside Ghostty (or any truecolor terminal) and to be readable over Tailscale SSH from a laptop or phone.
It is one of the three apps in this repository; it never writes state, only reads it and calls the brain's API.

```
┌ header ─────────────────────────────────────────────┐┌ 5 X-RAY ── mic ███████░░░ -17dB ┐
│ JARVIS [AUTONOMOUS] voice VOICE  ear local  ● live  ││ turn #t42 · 14 stages · done    │
│ budget ████░░░░░░░░░░░░░░░░ $15.76 / $200.00 (8%)   ││ heard  check why auth fails     │
│ turn #t42 "check why auth fails" · STT 188ms        ││ spoke  Found it — JWT expired.  │
├ 1 LIVE FLOW ────────────────────────────────────────┤│                                 │
│ 1 EAR/IO  2 VOICE IN  3 EXEC/OUT  4 GATEWAY  5 BRAIN││ ▎STT    +0.00s spk 0.78 · 188ms │
│ Local ●   Whisper ●   Mem0+Ctx ●  Server ●   db ●   ││ ▎INTENT +0.10s 94ms · User      │
│ RETURN ↩ agent output → SSE → retell → TTS          ││ ▎CLEAN  +0.20s 236ms · changed  │
├ 2 PAYLOAD ──────────────────────────────────────────┤│ ...                             │
│ Voice Ctx · Runner · Retell                         ││ EVENTS · all (v/b/e/A)          │
│ prepared request, memory, reasoning, runner output  ││   other turns & brain events    │
├ 3 TASKS ─────────────┬ 4 LEDGER ────────────────────┤│ DAEMON LOG · jarvis.err.log     │
│ DAG + metrics        │ Attempts │ Skills │ Outbox   ││   live stderr tail              │
└──────────────────────┴──────────────────────────────┘└─────────────────────────────────┘
 q quit  ? help  1-5 focus  z zoom  Enter inspect  p payload  v/b/e/A log filter  ...
```

Five panes: the left workspace holds Flow (`1`), Payload (`2`), Tasks (`3`), and Ledger (`4`); the far-right full-height sidebar (`5`) merges the Turn X-Ray, the filtered event stream, and the daemon log tail, with the live input volume meter anchored at the top-right corner.

## Build & run

```bash
cargo build --release --workspace   # ~2.7 MB binary, no runtime deps; target/release/jarvis-dashboard
jarvis dashboard                    # finds it on PATH, or in target/{release,debug}
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
| Live | `GET /v1/telemetry/events` (SSE, auto-reconnect) | Every stage of a voice turn (`voice.*`), the brain's route, phase transitions, `board.ts` RPC calls, streaming `pi` cost |

The dashboard never writes to `state.db`. Operator actions go through the authenticated loopback API
(`/v1/ask`, `/v1/outbox/{id}/resolve`, `/v1/runs/{id}/stop`).

### Pairing codes

When a client asks to pair (`POST /v1/pair/request`, which the mac app does on its first Connect), the daemon puts
`pair.requested` on the telemetry stream and the dashboard shows the six digits in its header and in a banner over the
panes, with a countdown, until the code is used (`pair.resolved`) or expires. The banner survives opening the dashboard
late, because the stream replays its ring buffer. Telemetry is the owner's token alone, which is what keeps a paired
device from reading a code out of it.

## Keys

| Key | Action |
| :--- | :--- |
| `q` / `Ctrl-c` | quit |
| `?` | keybinding overlay |
| `Tab`, `1`–`5` | focus Flow / Payload / Tasks / Ledger / X-Ray & Logs |
| `z` | zoom the focused pane to the full body |
| `j`/`k`, `g`/`G`, `PgUp`/`PgDn` | move selection / scroll |
| `[` / `]` | switch ledger tabs |
| `p` | cycle the payload tab: Voice Ctx · Runner · Retell |
| `v` / `b` / `e` / `A` | log filter: voice · brain · errors · all |
| `f` | toggle live follow on the newest turn |
| `Enter` | inspect a flow node · cycle the payload · open skill/task detail · load an attempt's transcript |
| `a` / `r` | approve / reject the selected queued outbox approval |
| `m` | toggle `ASSIST` ↔ `AUTONOMOUS` |
| `x` | stop the active run |
| `:` | send a natural-language instruction to `/v1/ask` |
| `y` | copy the focused content to the macOS clipboard |

## Diagnostics

```bash
jarvis-dashboard --render-check   # paint one frame into a TestBackend and print it (layout smoke test)
jarvis-dashboard --selftest 4     # run the real workers for 4s and print what arrived
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
| `src/ui/` | header/footer/modals, the flow map, the turn x-ray, the payload tabs, the log firehose, tasks, ledger |

## Tests

```bash
cargo test --workspace     # unit tests: config, model, session parser, HTTP client, tree flattening
cargo clippy --workspace --all-targets -- -D warnings
```
