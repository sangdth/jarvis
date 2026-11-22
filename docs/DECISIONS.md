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
- **The ear is one mic at a time, and it moves on speech, not on a button.** `Ear` (`remote_audio.py`) hands the ear
  over when a client connects, or when a mic hears speech while the live one has heard none for `HANDOVER_SECS`
  (1 s, `SWITCH_FRAMES` 60 ms of it). Limits: two Macs in one room both hear you, so the ear stays where it was —
  close the client on the one you are not using. A noisy room on the idle Mac can hold the ear with noise above
  `SPEECH_PEAK` (-30 dBFS). Nothing switches while Jarvis speaks (plus 0.5 s), which is what keeps one Mac's
  speaker from feeding another Mac's mic Jarvis's own words, but it also means you cannot move the ear mid-reply:
  talk again and it moves.
- **The relay port trusts a token, not the network.** It is plain HTTP and WebSocket with one shared secret
  (`REMOTE_TOKEN`, else `~/.config/jarvis/remote_token`). Anyone who reads the token, or the URL it rides in, can
  talk to Jarvis: with `MASTER_CHECK=false` that means run tasks. Keep it to your own network or a VPN, set your own
  token to re-pair, and delete the file to cut every client off.
- **One relay client at a time.** A second Mac is refused (1013) while one is connected; Jarvis has one ear.
  Connecting a new client after a drop does not replay what was said meanwhile.
- **The master check's voiceprint belongs to a mic.** `jarvis enroll` records with the Mac running Jarvis, so with
  `jarvis remote` the scores compare that voiceprint with another Mac's mic. Usually close enough; the
  `[Speaker] score=` lines say so, and `SPEAKER_THRESHOLD` is the fix. Enrolling through the relay is not built.

## Open risks

Not yet checked against real use:

- A Hermes event stream that drops mid-run in real life (the `GET /v1/runs/:id` fallback is only unit-tested).
- What Mem0 stores from a week of real conversation: duplicates and drift.
