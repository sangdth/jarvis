# Echo cancellation: talking over Jarvis on speakers

Researched 2026-11-04. Jarvis uses macOS voice processing (`src/jarvis/mac_audio.py`).

## The problem

On speakers the mic hears Jarvis. Whisper gets one long sentence that is mostly Jarvis's voice, the voice
check scores it low (0.08 in the 2026-11-04 19:34:40 log line) and drops it, and the user's "No, stop that."
inside it is lost.

## What was tried

| Option                          | Jarvis's echo         | User talking over Jarvis               |
| :------------------------------ | :-------------------- | :------------------------------------- |
| `voiceclean` 0.3.6              | not removed           | lost (transcript identical to raw mic) |
| WebRTC AEC3 (`livekit` 1.2.0)   | removed (~50 dB)      | damaged: "Still for that.", "Still good." |
| macOS voice processing (VPIO)   | removed               | heard: "No, stop that. No, stop that." |

Notes:
- `voiceclean` mutes a chunk only when the echo dominates it (`min_echo_ratio=0.5`); when both talk, neither
  dominates, so nothing is removed.
- The `voiceclean` and AEC3 rows come from a simulated mix (`say` voices, linear echo at three levels, Jarvis's
  own Whisper model). AEC3 removed Jarvis but also cut the user by 7–13 dB during double talk.
- The macOS row is a live test on a MacBook Pro, built-in mic and speakers at volume 44: the user said the
  phrase twice while the Jarvis line played. With processing off, the same test transcribed only Jarvis.
- Voiceprint scores through voice processing were lower (0.30–0.47 on clips of mostly silence) than raw speech
  (0.44–0.68 in logs), so `jarvis enroll` records through the same processing.

## Rules the engine needs (found while testing)

- Create the output side (`outputNode`, `mainMixerNode`) before `setVoiceProcessingEnabled`, or the engine fails
  to start with CoreAudio error -10875.
- One voice-processing engine per process: an engine started after it in the same process recorded 0 bytes.
- With processing on, the input shows as 9 identical channels at 48 kHz; channel 0 is used.
- A PyObjC tap block must return `None`; returning a value crashes the process.
- The player queues instantly, so the output transport paces itself (about 80 ms ahead) to keep interruptions fast.
