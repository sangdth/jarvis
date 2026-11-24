# Decisions

Choices that look like bugs but were made on purpose, with their known limits. Revisit one when its limit bites.

- **A pairing code is never sent to the client that asked for it.** The mac app has no secret before it pairs, so
  `POST /v1/pair/request` is open like `GET /client.py`; it answers only `{"ok": true, "expires_in": ...}` and the
  digits go to the cockpit's telemetry instead. That is why `/v1/telemetry/*` is now the owner's token alone (a
  paired device reads nothing there), and why a second request reuses the live code rather than minting another:
  a double click, or asking twice while you type, cannot steal the digits off the screen. Limits: whoever can read
  `jarvis top` can pair a device; with no cockpit open, `jarvis pair` in a terminal is the way to see a code.
- **The Connect app is a download from the gateway, not a release.** `apps/connect/build.sh` builds `Jarvis.app` on the
  checkout and the gateway serves it at `/Jarvis.app.zip` with `/install-app` beside it, so the app is never older
  than the code running on the Mac mini. Limits: `swiftc` is needed to build it; the bundle is signed ad-hoc when no
  Apple Development identity is in the keychain, so macOS can ask for the Microphone again after an in-app update.
- **One service command: `jarvis start`.** It syncs, builds the cockpit, builds the mac app, kicks launchd and opens
  the cockpit; `restart`, `update` and `upgrade` were separate spellings of parts of that and are gone. Limits: it is
  a restart, not "ensure running" — on a live instance it kills the voice mid-sentence. Run `jarvis` in a terminal,
  or `jarvis serve` for the stack without launchd, when you do not want it supervising.
- **One pending request per client** (T3 `runtime-request`, OpenCode `_pending_permission` / `_pending_form`).
  A second request before the first is answered replaces it. With one user answering by voice this has not
  happened; Claude Code serializes its own prompts with `_ask_lock`.
- **Hermes is gone.** The brain is the only orchestrator. The old separate Hermes Agent, its `HERMES_API_KEY` and
  its own guardian approvals were removed: a coding client's questions and approvals now arrive on the run's SSE
  stream and are answered deterministically, never by a model (`jarvis_core.questions`).
- **macOS voice processing needs a plain output device.** With a Multi-Output or aggregate device as the system
  output, the mic delivers nothing; `MacVoiceInputTransport` logs `SILENT_MIC_WARNING` after 3 s. To record both
  voices, capture system audio in the recorder (OBS, Screen Studio).
- **Jarvis speaks into Meet through a Chrome extension, not a virtual audio device.** `defaults/meet/jarvis.js`
  wraps Meet's `getUserMedia` and mixes in the speech `meet_voice.py` serves on `127.0.0.1:47123` (Meet's origin
  only). Limits: Chrome only, and Jarvis shares the user's seat, so a Meet mute mutes it too.
- **No Whisper vocabulary prompt.** An `initial_prompt` with project and agent names was tested on 2026-11-06: it
  leaked names into sentences that never said them and fixed none (`docs/research/voice-recognition-misses.md`,
  finding 6). Names are matched after Whisper, by the conversation model and `open_project`.
- **OpenCode's shared service streams every session.** Jarvis follows only sessions whose folder is the agent's `cwd`
  (`OpenCodeSSEAdapter._is_ours`, one lookup per session); a request from another project is never read out or
  answered. A server pinned with `OPENCODE_SERVER_PASSWORD` or a custom `url` is taken as one folder's, unfiltered.
- **One memory.** The brain runs in the same process as the voice, so what you tell Jarvis and what the board
  learned share one home; there is no second assistant with its own memory any more.
- **One ear, one mouth: a paired Mac takes them over, and there is no `jarvis remote` mode.** The relay routes are
  mounted on every run, because a mode made the common case (a Mac mini that only ever hears a laptop) a manual
  step, and left a paired laptop unable to connect to an already-running Jarvis. `MacVoiceTransport` holds the
  `RelayHub`: on attach it flushes this Mac's speaker, drops its mic frames and sends speech over the socket; on
  detach it takes both back. Limits: a mid-sentence handoff cuts that sentence, and meeting-mode call audio
  (`MeetingAudioBridge`) still comes from the host Mac, so guests are heard only while a client is not connected —
  a remote client is one ear, and the two sources are never mixed onto it.
- **Echo is cancelled on the Mac that speaks, and the relayed stream is resampled.** With the mic and speaker over
  the network, voice processing has to span the machine that plays the sound and the machine that hears it; it can't,
  so the client runs it and sends already-cancelled audio. Limits: echo cancellation is only as good as that Mac's
  speaker/mic pair, and anything that bypasses the client (another app on the Mac mini playing sound) is not
  cancelled. The pipeline's local output is 24 kHz and the thin client's wire format is 16 kHz, so `RelayHub`
  resamples on the way out rather than lowering the local speaker's quality or breaking clients already paired.
- **The relay port trusts a token, not the network.** It is plain HTTP and WebSocket with one shared secret
  (`REMOTE_TOKEN`, else `~/.config/jarvis/remote_token`). Anyone who reads the token, or the URL it rides in, can
  talk to Jarvis: with `MASTER_CHECK=false` that means run tasks. Keep it to your own network or a VPN, set your own
  token to re-pair, and delete the file to cut every client off.
- **One relay client at a time.** A second Mac is refused (1013) while one holds the mic; Jarvis has one ear.
  Connecting a new client after a drop does not replay what was said meanwhile.
- **The master check's voiceprint belongs to a mic.** `jarvis enroll` records with the Mac running Jarvis, so while a
  paired Mac is the mic the scores compare that voiceprint with that Mac's mic; the relay logs the warning when it
  connects. Usually close enough; the `[Speaker] score=` lines say so, and `SPEAKER_THRESHOLD` is the fix. Enrolling
  through the relay is not built.
- **Speaker names — and the owner — are inferred from conversation, not enrolled by interview.** With `MASTER_CHECK`
  on and no `owner_voice.npy` yet (or during `jarvis enroll`), the first person who addresses Jarvis locks onto their
  voice on turn one and saves `owner_voice.npy` once ~4 s across two turns is heard; background/TV speech ignored by
  the speech check never claims ownership. An introduction then offers a guest name to the room; the next unknown
  voice to answer gets it with a confidence (about 70% from adjacency alone, about 99% once it says "I'm David"), and
  only a name at 85% or more with at least 4 s of speech is written to `speakers/`. Unknown voices live in RAM for
  ten minutes, so TV never reaches the disk. Limits: if another person addresses Jarvis before the owner on a fresh
  install, run `jarvis enroll` to reset it; one label per sentence (no diarization); pitch only separates clear cases
  (~155–180 Hz overlaps).

- **The brain moved into this repository and was renamed.** Superbrain became `apps/brain` (package `brain`) with its
  full history via `git subtree`; the Rust cockpit came out to `apps/tui` and the two histories join at one merge.
  Limits: the old `sangdth/superbrain` repository is frozen (leave it read-only, do not push fixes there), and a task
  queued before the rename as `repo='superbrain'` no longer means "this checkout" — re-point it
  (`jarvis add --id <task> --repo brain …`) or it is treated as an ordinary folder under `projects_dir`.
- **`brain` is the name everywhere; the old `SUPERBRAIN_*` keys are still read, never written.** Settings are
  `BRAIN_HOME`, `BRAIN_HOST`, `BRAIN_PORT`, `BRAIN_API_KEY`, `BRAIN_URL`; the worker env vars are `BRAIN_WORKTREE_DIR`,
  `BRAIN_RPC_SOCK`, `BRAIN_TASK_ID`, `BRAIN_TASK_KIND`, `BRAIN_ALLOWED_REPOS`, `BRAIN_SKILLS_DIR`. Two reads stay for
  a running daemon or an old `.env`: `SUPERBRAIN_HOME` and `SUPERBRAIN_API_KEY` (Python and the cockpit, as fallbacks)
  and the state folder `~/.config/superbrain` until `jarvis init` moves it. The separate `brain` console script is
  gone: one command, `jarvis` (`jarvis serve|status|…`, or `uv run python -m brain.cli …` when working inside
  `apps/brain`).
- **Traces are local files plus one SQLite row, never an observability server.** `apps/brain/src/brain/traces.py`
  drains `TelemetryHub` in the background into `traces/<YYYY-MM-DD>/` (per-day folders, so retention is
  `shutil.rmtree`) and one `trace_runs` row with `git_sha`/`prompt_sha`, then writes `traces/analysis/` as the
  digest the offline `pi` Improver reads. `jarvis traces` and the cockpit's cold start read the same rows. Limits:
  token counts are only what a runner reports (0 today for voice turns), so `Succ/Mtok` is board-only until a
  runner surfaces usage; the cockpit's cold start synthesises `STT`/`ROUTE`/`RETURN` rather than parsing every
  trace file on each snapshot poll; and OTLP export is off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set
  (`shortcut:` OTLP/HTTP JSON over `httpx`, no protobuf/gRPC, to keep `grpcio` out of `.venv`).
- **The trace sink is inside the trust boundary, and protected from workers.** `traces.py` is in
  `PROTECTED_SELF_PATHS`, Seatbelt already denies `traces/` and `state.db*` to every worker but the Improver, and
  the sink redacts `pair.requested` codes, `SECRET_ENV` values and unaddressed `[Room]` speech before anything is
  written. Limits: a redaction bug is a data leak, not just a bad metric, so the sink's tests assert on bytes on disk.
- **One brain per machine, in this process.** `src/jarvis/supervisor.py` runs `brain.cli.running()` inside the  `jarvis` process and stops it at exit, so the voice and the board always share one home and one token; the
  one-mic lock (`~/.config/jarvis/jarvis.lock`) stops a second `jarvis` on the same machine. A client on another
  machine reaches that same process over `BRAIN_URL` (default `0.0.0.0:8643`). Limits: there is no attach mode, so
  a dev checkout runs its own brain; set `BRAIN_HOME` / `BRAIN_PORT` to keep its board and port apart from an
  installed one.
- **The checkout is the runtime; one home, one `.env`, one token.** Nothing is packaged: `install.sh` clones the
  repository, `uv sync`s it, and `~/.local/bin/jarvis` is a three-line shim into that checkout's `.venv`, so an
  edit plus `jarvis start` is live and there is no release to wait for. Limits: a checkout with uncommitted
  edits runs those edits (that is the point), so `jarvis --version` prints `+dirty` to make it obvious; two
  checkouts on one machine would each want the port, and the one-mic lock (`~/.config/jarvis/jarvis.lock`) stops
  the second.
- **One state home and one network surface.** The state is `JARVIS_HOME` (this checkout unless set, gitignored):
  `.env`, `SOUL.md`, `agents.json`, the voiceprint, models, memory, `brain/state.db`, and the tokens
  (`brain_token` 0600 for the apps, `remote_token` for a pinned relay). `BRAIN_API_KEY` in `.env` wins over the
  file. `jarvis init` (and the supervisor, on first run) moves a pre-merge `~/.config/superbrain` into
  `JARVIS_HOME/brain`. Everything network-shaped is one aiohttp app on `BRAIN_HOST:BRAIN_PORT`, defaulting to
  `0.0.0.0:8643` so a laptop or phone can reach the Mac mini; every route but `/health` needs the token, and
  `/v1/status`, `/v1/board`, `/v1/tasks`, `/v1/mode` and `/v1/restart` mean a client never reads SQLite or the
  filesystem. Limits: plain HTTP on the LAN, so the token is the only guard (put both machines on Tailscale to go
  further); a wildcard bind is reached over loopback on the same Mac, which both the Python and Rust clients
  handle.
- **Native workspaces, one task runner — not Bazel/Buck/Pants.** `uv` and `cargo` keep their own lockfiles, caches and
  native linking (MLX, sherpa-onnx, pyobjc, rusqlite), and `just` is the one entry point (`just test`, `just lint`,
  `just build`). Limits: there is no single cross-language dependency graph or remote cache, so a change to a shared
  schema is coordinated by the `schemas/` table and the commit, not enforced by a build tool. Revisit (moon/Nx, or
  Bazel) only if the repo grows to many apps with heavy CI fan-out.
- **The cockpit is the one compiled artifact.** `cargo build --release --workspace` in the checkout, and
  `jarvis top` looks on `PATH` and in `target/{release,debug}`. Limits: Python code is live as written, but a
  cockpit change needs that build; no toolchain means no cockpit, and the voice and the brain never depend on it.
- **`schemas/` documents contracts; the canonical files stay in their apps.** `schema.sql` stays in the Python package
  (the daemon reads it as package data at startup) and `openapi.json` describes the one gateway (HTTP, SSE, and the
  `/ws` relay). Limits: the
  Rust side queries tables by hand, so a column rename still needs a matching change in `apps/tui/src/db.rs`; a
  contract test on each side is what catches it.

- **The brain's memory is files, and only the offline Improver writes the top layer.** `INSTRUCTIONS.md` is the
  owner's and read-only to workers; `memory/MEMORY.md` is a <=150-line index injected with
  `--append-system-prompt`; details live in `memory/repos/<slug>.md` (<=80 lines). Only the `--offline` Improver
  writes those two, with an `expected_sha256` check, and progress notes record verified results as well as
  failures. Limits: a wrong note can survive until the next reflection pass, and the 150-line cap means some
  detail is only discoverable by opening a repo note.
- **Timers skip missed slots and never replay a burst.** A due cron timer advances to the next occurrence in its
  own IANA zone, and a crash between `firing` and `finish_timer` is recovered after a grace period; a schedule
  with no next occurrence ends `done`. Limits: an occurrence missed while the Mac is asleep is not delivered
  retroactively, and a `check` timer's exact crash window can lose or repeat one run.
- **The voice acknowledges an outbox item when `JarvisState` takes it, not when TTS finishes.** The item leaves
  `queued` and gets one line in `memory/reported.md` at that point; in `AUTONOMOUS` mode nothing is spoken until
  the owner switches back or asks for the brief. Limits: a crash between the acknowledgement and the speaker can
  lose one line (the alternative, repeated overnight speech, is worse).
- **The Improver may queue one bounded harness fix, never apply it or touch a protected file.** Its reflection
  prompt now carries the deterministic failure summary (`skills.mine_failure_patterns`: failed traces grouped by
  harness surface, plus `feedback/intent.jsonl` corrections), and `propose_harness_task` files one `repo='jarvis'`
  problem under `dedupe_key='harness-proposal'` with evidence and a reproduce-first `verify_cmd`. It stays a
  `draft` unless `AUTONOMOUS_HARNESS_UPGRADES=true`, so the owner still decides. Limits: one open proposal at a
  time; a fix that needs a protected file is refused and must be asked for by voice.
- **A harness proposal is remembered, checked and falsified.** `harness_proposals` holds one row per Improver fix:
  the surface it targets, its spec hash, the failure rate measured from `trace_runs` before it landed, and what
  became of it. `propose_harness_task` refuses an idea already `rejected`, `failed_verify`, `rolled_back` or
  `falsified` within 30 days (same spec hash, or word overlap `>= 0.8`), and K=10 runs on the surface after the fix
  either verify the prediction or mark it falsified and offer a rollback quoting baseline vs post rate. Limits: only
  `pushed` and `failed_verify` move a proposal automatically in `tick`; a 2-crash rollback stays the sha circuit
  breaker's job, and the K window counts any run on the surface, not only the ones the fix was meant to change.
- **The Improver sits on a 7-day probation when a self-upgrade edits it.** A verified upgrade touching
  `skills.py` or `reconciler.py` records `improver_rollback_sha` and a baseline of `compute_meta_utility()` (first-
  attempt builder pass rate, interactive friction per 100 turns, median USD and turns per solved task, all computed
  from `attempts` + `trace_runs`). Once 10 tasks and 30 turns have run under it, a `-10 pp` pass rate, `+5` friction
  or `+35%` cost queues a rollback approval. Limits: the thresholds are a first guess, and a quiet week with little
  interactive use simply keeps the probation open until its date passes.
- **A self-upgrade that touches the voice runs its benchmark.** `_changed_files` + `_run_voice_benchmarks` run
  `scripts/bench_intent.py 1` (≥97%) for `executive_filter.py` / `languages.json`, and `scripts/bench_routing.py 1`
  (≥95%) for `prompts.py`, `agent_tools.py` or `orchestrator.py`. The script is read from the checkout, not the
  candidate, so a worker cannot rewrite the gate it is measured by; the candidate's voice modules load through
  `PYTHONPATH`. Both benchmarks and the scripts themselves are in `PROTECTED_SELF_PATHS`. Limits: the benchmark
  process gets `OPENROUTER_API_KEY` (it makes paid model calls that the worker's cost ledger does not see), a
  failing gate is reported as sealed output like the held-out suite, and without a key the gate is skipped with a
  blocker rather than blocking the daemon.

## Open risks

Not yet checked against real use:

- Whether `bge-m3` cosine `>= 0.88` separates distinct hypotheses the way the sample suggested: the numeric-constant
  exemption keeps sweeps working, but a threshold change would silently widen or narrow what the tournament accepts.
- The 7-day Improver probation needs a week of real use before its thresholds (`-10 pp` pass rate, `+5` friction per
  100 turns, `+35%` cost) can be called calibrated.
- A run's SSE stream that drops mid-run in real life (the `GET /v1/runs/:id` fallback is only unit-tested).
- T3's worktree `workspaceStrategy` fields against a live T3 build: the automatic thread path uses `root`, and the
  board still builds on `pi`, so no board task depends on the worktree variant yet.
- What Mem0 stores from a week of real conversation: duplicates and drift.
