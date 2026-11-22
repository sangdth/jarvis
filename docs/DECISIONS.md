# Decisions

Choices that look like bugs but were made on purpose, with their known limits. Revisit one when its limit bites.

- **One pending request per agent** (OpenCode `_pending_permission` / `_pending_form`, Hermes `_pending_approval`).
  A second request before the first is answered replaces it. With one user answering by voice this has not
  happened; Claude Code serializes its own prompts with `_ask_lock`.
- **Hermes approves its own commands.** Its guardian model ran `rm -rf` on a test folder without asking during an
  integration run. Only commands it flags reach Jarvis. Tightening is a Hermes setting (`approvals` in
  `~/.hermes/config.yaml`), not Jarvis code.
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
- **Two memories.** Hermes keeps its own; what you tell Hermes is not in Jarvis's Mem0, and the other way round.
- **`jarvis remote` cancels echo on the other Mac, not here.** With the mic and speaker over the network, the voice
  processing has to span the machine that plays the sound and the machine that hears it; it can't, so the client
  runs it and sends already-cancelled audio. Limits: echo cancellation is only as good as that Mac's speaker/mic
  pair, and anything that bypasses the client (another app on the Mac mini playing sound) is not cancelled.
- **The relay port trusts a token, not the network.** It is plain HTTP and WebSocket with one shared secret
  (`REMOTE_TOKEN`, else `~/.config/jarvis/remote_token`). Anyone who reads the token, or the URL it rides in, can
  talk to Jarvis: with `MASTER_CHECK=false` that means run tasks. Keep it to your own network or a VPN, set your own
  token to re-pair, and delete the file to cut every client off.
- **One relay client at a time.** A second Mac is refused (1013) while one holds the mic; Jarvis has one ear.
  Connecting a new client after a drop does not replay what was said meanwhile.
- **The master check's voiceprint belongs to a mic.** `jarvis enroll` records with the Mac running Jarvis, so with
  `jarvis remote` the scores compare that voiceprint with another Mac's mic. Usually close enough; the
  `[Speaker] score=` lines say so, and `SPEAKER_THRESHOLD` is the fix. Enrolling through the relay is not built.

- **Superbrain moved into this repository (`apps/superbrain`), and it moved with its history.** `git subtree` at
  `origin/master` kept every commit, the Rust cockpit came out to `apps/tui`, and the two repositories' histories now
  join at one merge. Limits: the old `sangdth/superbrain` repo is frozen (leave it read-only, do not push fixes there),
  and a task already queued as `repo='superbrain'` still resolves through the alias in `config.SELF_REPOS`.
- **One brain per machine, attach or start.** `src/jarvis/supervisor.py` probes `SUPERBRAIN_URL` with the shared token:
  a daemon answering means attach (the 24/7 LaunchAgent keeps overnight work alive across voice restarts), nothing
  answering means run `superbrain.cli.running()` in this loop and stop it at exit. Limits: a dev checkout on a Mac
  with the daemon running attaches to that daemon (with `MASTER_CHECK` off, that means the dev voice can queue work on
  the live board); set `SUPERBRAIN_URL`/`BRAIN_PORT` on the dev side to keep them apart. A token mismatch stops Jarvis
  with a named error instead of silently starting a second daemon.
- **One home, one `.env`, one token.** The brain's state is `JARVIS_HOME/brain` and the bearer token is
  `JARVIS_HOME/brain_token` (0600), shared by the voice, the daemon and the cockpit; `SUPERBRAIN_API_KEY` in `.env`
  wins over it. `jarvis init` moves a pre-merge `~/.config/superbrain` into place. Limits: until it is moved, an
  installed Jarvis keeps using the old folder (`brain_home()` prefers it), so two paths exist for a while; the Rust
  cockpit mirrors the same rule. Keys `HOST`/`PORT` in an old `.env` are no longer read (the defaults are
  `127.0.0.1:8643`); the settings are `BRAIN_HOST`/`BRAIN_PORT`, so a stray `PORT=3000` exported for a web dev server
  cannot move the daemon.
- **Native workspaces, one task runner — not Bazel/Buck/Pants.** `uv` and `cargo` keep their own lockfiles, caches and
  native linking (MLX, sherpa-onnx, pyobjc, rusqlite), and `just` is the one entry point (`just test`, `just lint`,
  `just build`). Limits: there is no single cross-language dependency graph or remote cache, so a change to a shared
  schema is coordinated by the `schemas/` table and the commit, not enforced by a build tool. Revisit (moon/Nx, or
  Bazel) only if the repo grows to many apps with heavy CI fan-out.
- **The cockpit is a binary of its own.** `cargo install --git ... --bin jarvis-top` (the installer does it when a
  Rust toolchain is present; `jarvis upgrade` rebuilds it), and `jarvis top` looks on `PATH` and in
  `target/{release,debug}`. Limits: no toolchain means no cockpit; the voice and the brain never depend on it, and a
  failed cockpit build only warns.
- **`schemas/` documents contracts; the canonical files stay in their apps.** `schema.sql` stays in the Python package
  (the daemon reads it as package data at startup) and `openapi.json` describes the server the daemon runs. Limits: the
  Rust side queries tables by hand, so a column rename still needs a matching change in `apps/tui/src/db.rs`; a
  contract test on each side is what catches it.

## Open risks

Not yet checked against real use:

- A Hermes event stream that drops mid-run in real life (the `GET /v1/runs/:id` fallback is only unit-tested).
- What Mem0 stores from a week of real conversation: duplicates and drift.
