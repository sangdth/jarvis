# jarvis-top — the cockpit (Ratatui)

A single-binary, fullscreen terminal dashboard for Jarvis's brain (`apps/brain`). Designed to run 24/7 on a
machine inside Ghostty (or any truecolor terminal) and to be readable over Tailscale SSH from a laptop or phone.
It is one of the three apps in this repository; it never writes state, only reads it and calls the brain's API.

```
┌ header ──────────────────────────────────────────────────────────────────────────────┐
│ JARVIS [AUTONOMOUS] voice VOICE  check master check  ear local + paired  ● live       │
│ budget ████░░░░░░░░░░░░░░░░░░░░░░ $15.76 / $200.00 (8%)  [! 1 APPROVAL]              │
│ turn #42 "check why the auth test is failing" · STT 188ms · INTENT 94ms · LLM 410ms  │
├ 1 LIVE FLOW ─────────────────────────────────────────────────────────────────────────┤
│ 1 EAR/IO      2 VOICE IN      3 EXEC/OUT     4 GATEWAY      5 BRAIN                  │
│ Paired Mac ●  Whisper+Spk ●   Mem0+Ctx ●     BrainServer ●  state.db ●               │
│ Meet / CLI ○  Intent+Cln ●    Exec LLM ●     Router ●       Reconciler ●             │
│               ...             State+TTS ●    T3 / Pi ●      Improver/Out ○            │
│ RETURN ↩ agent output → SSE → AGENT_REPORT_PROMPT → retell → TTS                      │
├ 2 TURN X-RAY ──────────┬ 3 PAYLOAD ─────────────┬ 4 LOGS ───────────────────────────┤
│ heard/spoke            │ Voice Ctx · Runner ·   │ voice.stt · voice.intent ·        │
│ STT INTENT CLEAN MEM   │ Retell                 │ brain.route · … filtered          │
│ PREP THINK TOOL SEND   │ prepared request,      ├ daemon log tail ──────────────────┤
│ RETURN RETELL TTS LAT  │ memory, reasoning      │ jarvis.err.log                    │
├ 5 TASKS ───────────────┴ 6 LEDGER ───────────────────────────────────────────────────┤
│ DAG + metrics          │ Attempts │ Skills │ Outbox │ Notes │ DB & Logs             │
└───────────────────────────────────────────────────────────────────────────────────────┘
 q quit  ? help  1-6 focus  z zoom  Enter inspect  p payload  v/b/e/A log filter  ...
```

Six panes, because one screen should answer "what did I say, what did Jarvis do with it, what came back". Pane 1 is
the flow map: the ear, the voice checks, the conversation model, the gateway and the brain, with the wire into a
stage lit while that stage works and the return path spelled out underneath. Pane 2 is one turn stage by stage,
every line a real telemetry event. Pane 3 shows the payload behind those stages: the cleaned sentence, the memories
injected, the prepared request, the model's reasoning, the runner's output and the retell. Pane 4 is the firehose.

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
| Live | `GET /v1/telemetry/events` (SSE, auto-reconnect) | Every stage of a voice turn (`voice.*`), the brain's route, phase transitions, `board.ts` RPC calls, streaming `pi` cost |

The dashboard never writes to `state.db`. Operator actions go through the authenticated loopback API
(`/v1/ask`, `/v1/outbox/{id}/resolve`, `/v1/runs/{id}/stop`).

### Pairing codes

When a client asks to pair (`POST /v1/pair/request`, which the mac app does on its first Connect), the daemon puts
`pair.requested` on the telemetry stream and the cockpit shows the six digits in its header and in a banner over the
panes, with a countdown, until the code is used (`pair.resolved`) or expires. The banner survives opening the cockpit
late, because the stream replays its ring buffer. Telemetry is the owner's token alone, which is what keeps a paired
device from reading a code out of it.

## Keys

| Key | Action |
| :--- | :--- |
| `q` / `Ctrl-c` | quit |
| `?` | keybinding overlay |
| `Tab`, `1`–`6` | focus Flow / X-Ray / Payload / Logs / Tasks / Ledger |
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
| `src/ui/` | header/footer/modals, the flow map, the turn x-ray, the payload tabs, the log firehose, tasks, ledger |

## Tests

```bash
cargo test --workspace     # unit tests: config, model, session parser, HTTP client, tree flattening
cargo clippy --workspace --all-targets -- -D warnings
```
