# Adopting Hex ideas in Jarvis: plan (reviewed 2026-11-08)

Companion to `hex-architecture-analysis.md`. Every idea below was checked against the Jarvis code, Pipecat 1.15.0's
source in `.venv`, the Hex source (`~/Projects/others/hex/src`), the live OpenCode 2.2.5 service on this Mac, and
`voice-recognition-misses.md` before it kept its place. The rule it keeps: Jarvis stays thin; it routes and relays, and
the work lives in each agent's own harness.

## Short version

Of the seven ideas in the first draft, one survives and is expanded to fix two shared-daemon bugs, one is reshaped
around how `SpeakerVerifiedWhisperSTT` actually drops frames, and five are cut or parked:

| Idea (first draft)                    | Verdict   | Why, in one line                                             |
| :------------------------------------ | :-------- | :----------------------------------------------------------- |
| OpenCode `service.json` discovery     | Do first  | A `--service` daemon runs on port 49374; Jarvis tries 4096   |
| Audio fixture env var                 | Reshaped  | Save segments before any drop first; replay only if needed  |
| Whisper `initial_prompt` vocabulary   | Cut       | Rejected 2026-11-06 (misses research, finding 6)             |
| Sub-160 ms and DC-offset gate         | Cut       | Four existing gates already drop clicks and hallucinations   |
| `utterances.jsonl` flight recorder    | Cut       | The terminal log already records every gate's verdict        |
| Foreground app and window context     | Parked    | Accessibility permission; the newest-repo rule covers it     |
| `hex mcp` meetings bridge             | Cut       | Hex isn't installed; it's Hermes config, not Jarvis code     |

## Steps

Smallest first. Each: what breaks, what to change, how to check. Nothing starts before Sang's go-ahead.

### 1. Reach the OpenCode server that is already running (and scope it to `cwd`)

What breaks: `README.md` asks for a second server, `OPENCODE_SERVER_PASSWORD=… opencode serve --port 4096`, started in
the project folder with the same password in `.env`. OpenCode 2 registers the daemon it already runs
(`opencode serve --service`, started by launchd in `/Users/sang`; today `http://127.0.0.1:49374`) in
`~/.local/state/opencode/service.json`, mode 0600, with `id`, `version`, `url`, `pid` and `password`. Jarvis's default
`url` is port 4096, so it never finds that one, and a wrong password shows only as a debug line (`OpenCode stream
unavailable`, retried every 10 s).

Hex: `src/desktop/opencode_service.rs` reads `$XDG_STATE_HOME/opencode/service.json` (else `~/.local/state/opencode`),
opens it with `O_NOFOLLOW` and checks `uid == geteuid()` and `mode & 0o077 == 0`, refuses non-loopback URLs (so a file
can never make the client send the password off-machine), and never starts OpenCode itself.

Checked live against OpenCode 2.2.5 (`pid 95993`, port 49374) on 2026-11-08, which uncovered four details the
adapter must handle when talking to the shared `--service` daemon instead of a per-folder `opencode serve`:

1. **Session directory:** The daemon runs in `/Users/sang`, so `POST /api/session` with `{"title": "Jarvis"}` creates a
   session in `/Users/sang` (`x-opencode-directory` is ignored on session creation). Passing
   `"location": {"directory": str(Path(self.config.cwd).expanduser().resolve())}` creates the session in the configured
   project folder (a relative `"."` returns HTTP 500; it must be resolved).
2. **`_resync()` directory scoping:** `GET /api/permission/request` and `GET /api/form` filter by the directory of the
   request. Without `x-opencode-directory: <resolved cwd>`, `_resync()` queries `/Users/sang` and misses pending
   permissions and forms in the project folder. (`GET /api/event` is global and needs no header.)
3. **Log message on `self.base_url`:** `_run_loop` logs `f"Connected to OpenCode event stream at
   {self.base_url}/api/event"`, which would still print port 4096 if only `self._http.base_url` were updated.
4. **Session latching (the bug that matters):** `_handle_event` runs `self._session_id = self._session_id or
   session_id` on every event, and the daemon's `GET /api/event` streams every session on the machine (50 today,
   across many folders; the user's own OpenCode app included). The first `session.*` event from any project would
   lock Jarvis to that session; `permission.asked` and `form.created` from any project would be read out, and
   `answer_agent` would approve them; `stop_agent` would interrupt a run in another project. Nothing in the event
   says which folder: `Permission.Request` and `Form.Info` carry only `sessionID`, and the envelope is
   `{"id", "type", "data"}`. `GET /api/session/{id}` returns `location.directory`, so one lookup per unknown session
   settles it. With a per-folder `opencode serve` this latch was harmless; with the shared daemon it widens what
   Jarvis can approve to everything OpenCode runs.

Change, in `opencode_adapter.py`:

- `discover_service() -> tuple[str, str] | None`: read `$XDG_STATE_HOME/opencode/service.json` (else
  `~/.local/state/opencode/service.json`); check `not path.is_symlink()`, `st.st_uid == os.getuid()`,
  `stat.S_IMODE(st.st_mode) & 0o077 == 0`, and that `urlparse(url)` is `http` on `127.0.0.1`, `localhost`, or `::1`;
  return `(url.rstrip("/"), password)`. One `shortcut:` comment: `service.json` only, no `service-<channel>.json`,
  until a beta channel is in use.
- Precedence in `_run_loop` before each connect attempt:
  - When `OPENCODE_SERVER_PASSWORD` is set in `.env`, or `config.url` is neither missing nor the shipped default
    (`config.url not in (None, "http://127.0.0.1:4096")`), keep that URL and password.
  - Otherwise, if `discover_service()` returns `(url, password)`, set `self.base_url = url`,
    `self._http.base_url = url`, and `self._http.auth = ("opencode", password)`. Done per attempt, not once at startup,
    because a restarted OpenCode picks a new port. A stale file fails to connect on `/api/event` and retries in 10 s, so
    no separate `/api/info` round-trip is needed.
- Resolve `self._cwd = str(Path(config.cwd).expanduser().resolve())` in `__init__`:
  - Pass `headers={"x-opencode-directory": self._cwd}` on the `GET /api/permission/request` and `GET /api/form` calls in
    `_resync()`.
  - Pass `"location": {"directory": self._cwd}` in `POST /api/session` inside `send_task()`.
- Drop the latch in `_handle_event`. A session is Jarvis's when `send_task()` created it, when the directory-scoped
  `_resync()` returned its request, or when a `permission.asked`, `form.created` or `session.*` event names an unknown
  session and `GET /api/session/{id}` says `location.directory == self._cwd`. Cache the verdict per session id (a
  `dict[str, bool]`), so each session costs one lookup; a failed lookup counts as not ours. Events from sessions in
  other folders are ignored, the way child-session events already are.
- `README.md` section 4, `defaults/env.example` and the `AGENTS.md` "Needs" line: note that Jarvis finds the running
  OpenCode 2 service automatically, and `OPENCODE_SERVER_PASSWORD` is only for pinning a separate `opencode serve`.

Check: `tests/test_opencode_adapter.py` with `XDG_STATE_HOME` pointed at a temp folder: a 0600 loopback file sets
`base_url` and `auth`, a 0644 file or non-loopback URL is ignored, no file falls back to `config.url`, a set
`OPENCODE_SERVER_PASSWORD` wins, `_resync()` sends `x-opencode-directory`, `send_task()` sends the resolved
`location.directory`, a `permission.asked` from a session in another folder is neither surfaced nor adopted, and one
in `cwd` is adopted after a single `GET /api/session/{id}` (mocked). Live: with `OPENCODE_SERVER_PASSWORD` unset,
run `uv run jarvis`, confirm the log says
`Connected to OpenCode event stream at http://127.0.0.1:49374/api/event`, and send OpenCode a task.

About 45 lines in `opencode_adapter.py`, plus 40 of tests.

### 2. Keep the audio Whisper heard (including segments Whisper or the speaker check dropped)

What breaks: a miss leaves only text in the log, or nothing at all when Whisper returns no text. The 2026-11-06
research
had to fake voices with macOS `say` to measure anything, and its "enroll with short phrases too" idea (finding 3) is
still untested because no real clip of a dropped "Hello?" (`0.26`) or "What's he going on?" (`0.23`) exists.

Hex: `HEX_RETAIN_DICTATION_AUDIO=<count>` (`src/dictation/diagnostics.rs`) saves owner-only WAVs (`0700` folder, `0600`
files, rotated to keep at most `<count>` clips) for both transcribed and discarded clips.

Pipeline subtlety in Jarvis: `SpeakerVerifiedWhisperSTT.run_stt` (`voice_gates.py:48-59`) loops over the frames
`super().run_stt(audio)` yields. When Whisper drops a segment via `no_speech_prob >= 0.6` or `compression_ratio`,
`super().run_stt(audio)` yields **zero frames**—the `for frame in frames:` loop in `SpeakerVerifiedWhisperSTT` never
executes, and not even `[Speaker] score=` is logged. Saving inside that loop would miss every Whisper-dropped clip.

Change: in `SpeakerVerifiedWhisperSTT.run_stt` (`voice_gates.py`), right after `score = await score_task` (outside the
`for frame in frames` loop), when `JARVIS_SAVE_SEGMENTS` is set to a positive integer `N` (for example
`JARVIS_SAVE_SEGMENTS=200`):

- Write `audio` (16 kHz mono PCM16, with Pipecat's 1 s pre-roll and 0.5 s trailing silence) via stdlib `wave` to
  `JARVIS_HOME/clips/<YYYYmmdd-HHMMSS-fff>.wav` (`0700` dir, `0600` file) and unlink oldest `.wav` files past `N`.
- Include the clip filename on the `[Speaker] score=…` log line (and log `[Speaker] score=… no-text (<clip>)` when
  `frames` is empty so silent Whisper drops become visible too).
- Add `/clips/` to `.gitignore`; log at startup in `main.py` when `JARVIS_SAVE_SEGMENTS` is active.

Check: a unit test in `tests/test_voice_gates.py` with `WhisperSTTServiceMLX.run_stt` faked (both yielding a frame and
yielding nothing): verifies the WAV frame count, `0600` permissions, and rotation at `N`. Live: run one evening with
`JARVIS_SAVE_SEGMENTS=200`; re-score real missed clips with `mlx_whisper.transcribe` and `SpeakerEmbedder.embed`.

About 20 lines in `voice_gates.py`, plus 20 of tests. Privacy: your voice stays in `JARVIS_HOME/clips` (`0700`), bounded
to `N` files (~40 MB at `N=200`), off by default.

### 3. Replay a clip through the live pipeline (only if step 2 is not enough)

What breaks: step 2's clips can be re-transcribed and re-scored offline, but VAD splitting (`stop_secs=0.6`), the
trailing-off stitcher (`HOLD_SECS=2.0`), and `ExecutiveFilter` timing can only be exercised through the full Pipecat
pipeline.

Pipeline subtlety in Jarvis: `MacVoiceAudio.__init__` (`mac_audio.py:50-68`) unconditionally initializes
`AVFoundation.AVAudioEngine` and enables macOS voice processing on `inputNode()`, and `MacVoiceOutputTransport` shares
that same `self._audio` instance for speaker playback. Simply skipping the mic tap in `MacVoiceInputTransport.start`
still wakes the hardware microphone when `self._audio.start()` runs for the output transport.

Change (only if needed after step 2): in `MacVoiceTransport` / `MacVoiceInputTransport` (`mac_audio.py`), when
`JARVIS_AUDIO_FIXTURE` names a WAV file (any rate, mono or stereo, float or PCM16 via `wave`):

- Do not install the input tap or run `_warn_if_silent()`; feed decoded samples in 20 ms blocks at the WAV's native
  `sample_rate` into `self._on_mic(block, wav_rate)` at real-time pace (`asyncio.sleep`), so `MicConverter` exercises
  the exact same resampling and `SILENT_MIC_PEAK` path as hardware audio, then stop at EOF instead of looping.

Check: `tests/test_mac_audio.py`: a generated 100 ms WAV pushes five 20 ms `InputAudioRawFrame`s without touching
`AVFoundation`. Live replays stay out of `tests/` (`AGENTS.md`).

About 25 lines plus a test. Park until step 2 produces a clip whose offline Whisper + speaker score does not explain
the miss.

### 4. Write the outcome down

In the same turn as step 1 lands: `AGENTS.md` "Later, not now" gets one line pointing here for the parked ideas, and
`docs/DECISIONS.md` gets "No Whisper vocabulary prompt" citing the 2026-11-06 result so nobody proposes it a third time.

## Cut, with the evidence

### Whisper `initial_prompt` with project and agent names

Tested on 2026-11-06 (`voice-recognition-misses.md`, finding 6) with `"Jarvis, T3 Code, Claude Code, OpenCode, Hermes,
Hugger Hustle, Happy Voxel, OpsCom, Neuroparticles."` on both models: distil changed nothing useful; turbo leaked prompt
words into a sentence that never said them (`OpenCode, OpsCom, Project and Tasker East for a status update.`), turned
`hello` into `mop mop mop`, and made noise into `Thank you.`; `T3` and `Hugger` stayed wrong with and without it. Hex
knows the failure: `drop_prompt_echo` in `src/transcription/gguf_session.rs` exists because Whisper models "can return
their context prompt verbatim on silence or noise". Pipecat's `WhisperMLXSTTSettings` has no `initial_prompt` field
either (only `WhisperSTTService` for faster-whisper does, `stt.py:285`), so passing one would also mean copying
`WhisperSTTServiceMLX.run_stt` (`stt.py:575-638`). Names are already resolved after Whisper: the system prompt says
"speech recognition mishears names, pick the closest one" and `open_project` matches the folder case-blind.

### A duration and DC-offset gate before Whisper

Already there four times over:

1. Silero VAD confirms speech only after `start_secs=0.2` at `confidence=0.7` and `min_volume=0.5` (`main.py:224`), and
   `SegmentedSTTService` wraps that with 1.0 s of pre-roll (`stt_service.py:1012`) plus 0.5 s of trailing silence
   (`stt_service.py:949`), so `run_stt` never sees a raw <160 ms buffer.
2. `WhisperSTTServiceMLX.run_stt` (`stt.py:610-619`) drops segments with `no_speech_prob >= 0.6` or the
   `compression_ratio == 0.5555555555555556` hallucination signature.
3. `SpeakerEmbedder.embed` (`speaker.py:56-75`) runs `trim_silence()` (within 20 dB of peak) and returns `None` when
   `!is_ready(stream)`, which drops the sentence (`score = 0.0`).
4. `classify_fast_command` (`state.py`) drops `_WHISPER_FILLERS` (`"okay"`, `"ok"`, `"thanks"`, `"thank you"`, `"you"`,
   `"bye"`).

Hex needs its own `speech_detector.rs` (160 ms at 0.4) and `pause_segmenter.rs` (DC-removed level) because Hex records
raw hotkey holds without a VAD gate in front of capture.

### `utterances.jsonl`

The terminal log already carries each gate's verdict per sentence: `[Speaker] score=… pass|drop`, `holding for the
rest`, `[User Transcript]`, `Ignored self-talk`; the 2026-11-06 research was written from those lines alone, and
`AGENTS.md` names the log as the first stop. A second file adds a place to look, and Hex's NDJSON
(`src/desktop/events.rs`) is not bounded on disk either (only its reader has `MAX_EVENT_RECORD_BYTES`). The one missing
number is how long the intent check took, for the "slow reply" question (finding 7): a `logger.info` with a
`time.monotonic()` difference in `_gate`, three lines, when that question comes up.

### `hex mcp` for meetings

No `hex` binary on this machine and no release build in `~/Projects/others/hex`; nothing to bridge. When Hex is in use,
`hex mcp` is a read-only stdio server (`src/meeting/mcp.rs`) that belongs in Hermes's or T3's MCP config, not in Jarvis
(thin layer).

## Parked

### Foreground app and window title as routing context

Hex reads the front app from `NSWorkspace` (no permission) and the window title through the Accessibility API
(`AXFocusedWindow` in `src/accessibility.rs`, a permission the user must grant), and treats titles as sensitive
(`context.rs`: "a window title can hold an email subject"). For Jarvis the front app during hands-free use is often a
browser, Slack or T3 itself, whose title is the thread, not the folder; `project_list` already says "where I left off"
means the newest-touched repo; and a new line in the system prompt requires a `bench_routing.py` run (≥95%). Revisit
only if real logs show the user saying "here" or "this project" and Jarvis asking which.

### Ideas with nothing to adopt

- Streaming transcription during capture: Pipecat's MLX service is segment-based, the speaker check needs the whole
  sentence anyway, and the 8.8 s reply in finding 7 was LLM (2.0 s) and TTS (6.4 s) time, not Whisper's 0.6 s.
- Warm pre-roll: `SegmentedSTTService` (`stt_service.py:1012`) already keeps the last 1 s before speech starts.
- Echo classification (`src/meeting/echo.rs`): Jarvis cancels its own speaker output in real time with macOS voice
  processing (`mac_audio.py`); Hex classifies two recorded meeting tracks after the fact (Hex has no voice output).
- QuickJS user transforms: the agents own logic and tools; Jarvis stays thin.
- Hugging Face cache scan for models: `mlx_whisper` already loads from `~/.cache/huggingface/hub`.
- Hotkey, HUD, chimes: Jarvis is ambient and answers with a voice. A "heard you" beep before a slow reply is one
  possible answer to finding 7; `on_beep` exists, so it is two lines if Sang wants to try it.

## Risks to decide on

- Step 1 changes which server Jarvis talks to when `OPENCODE_SERVER_PASSWORD` is empty and `url` is the default: from
  an unauthenticated localhost:4096 fallback to the daemon OpenCode registered in `service.json`, with new sessions and
  resyncs scoped to `config.cwd`. Setting `OPENCODE_SERVER_PASSWORD` or a custom `url` keeps the old behavior. The
  session-latch fix is not optional: without it, step 1 lets Jarvis approve tool calls in every OpenCode session on
  the machine. The fix also changes today's behavior for a per-folder server: a session Jarvis neither created nor
  found pending at connect time is adopted only after its folder is confirmed.
- Step 2 writes your voice (and room audio that triggered VAD) to `JARVIS_HOME/clips/`; off unless
  `JARVIS_SAVE_SEGMENTS` is set, mode `0700`/`0600`, rotated at `N` files.
- Neither step changes reply latency (finding 7) or the speaker threshold on short phrases (finding 3) directly—step 2
  is what gives us real clips of dropped short phrases to test finding 3 without guessing.
