# Decisions

Choices that look like bugs but were made on purpose, with their known limits. Revisit one when its limit bites.

- **One pending request per agent** (OpenCode `_pending_permission` / `_pending_form`, Hermes `_pending_approval`).
  A second request before the first is answered replaces it. With one user answering by voice this has not
  happened; Claude Code serializes its own prompts with `_ask_lock`.
- **Hermes approves its own commands.** Its guardian model ran `rm -rf` on a test folder without asking during an
  integration run. Only commands it flags reach Jarvis. Tightening is a Hermes setting (`approvals` in
  `~/.hermes/config.yaml`), not Jarvis code.
- **macOS voice processing needs a plain output device.** With a Multi-Output or aggregate device (BlackHole) as
  the system output, the mic delivers nothing; `MacVoiceInputTransport` logs `SILENT_MIC_WARNING` after 3 s. To
  record both voices, capture system audio in the recorder (OBS, Screen Studio) instead of routing through BlackHole.
- **No Whisper vocabulary prompt.** An `initial_prompt` with project and agent names was tested on 2026-11-06: it
  leaked names into sentences that never said them and fixed none (`docs/research/voice-recognition-misses.md`,
  finding 6). Names are matched after Whisper, by the conversation model and `open_project`.
- **OpenCode's shared service streams every session.** Jarvis follows only sessions whose folder is the agent's `cwd`
  (`OpenCodeSSEAdapter._is_ours`, one lookup per session); a request from another project is never read out or
  answered. A server pinned with `OPENCODE_SERVER_PASSWORD` or a custom `url` is taken as one folder's, unfiltered.
- **Two memories.** Hermes keeps its own; what you tell Hermes is not in Jarvis's Mem0, and the other way round.

## Open risks

Not yet checked against real use:

- A Hermes event stream that drops mid-run in real life (the `GET /v1/runs/:id` fallback is only unit-tested).
- What Mem0 stores from a week of real conversation: duplicates and drift.
