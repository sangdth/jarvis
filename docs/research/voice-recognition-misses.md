# Voice recognition misses: why Jarvis asks to repeat (research, 2026-11-06)

Goal: find out why the user has to repeat themselves. Sources: the pipeline code at `2472aff` (`mac_audio.py`,
`voice_gates.py`, `speaker.py`, `executive_filter.py`, `state.py`, `main.py`), Pipecat 1.15.0's `SegmentedSTTService`
and `WhisperSTTServiceMLX`, the T3 terminal logs in `~/.t3/userdata/logs/terminals/*.log` (2026-11-02 to 11-06), and
one synthetic-speech test (below).

Short version: the logs show three things. Whisper garbles agent and project names with both models, and the speech
check then treats the garbled sentence as background, most often when it is an answer to a question Jarvis just
asked. Short phrases ("Hello?", "hello") score under the voice threshold, and padding is not the reason. On 11-06 a
reply took 8.8 s to start, which feels like not being heard. The installed Jarvis and the dev checkout also run
different settings, so the logs mix setups.

## Where a sentence can be lost

```
Mic (macOS voice processing, 48 kHz -> 16 kHz)
  -> 1. Silero VAD in the user aggregator (confidence 0.7, min_volume 0.5, start 0.2 s, stop 0.6 s)
  -> 2. MLX Whisper, language fixed to English (segments with no_speech_prob >= 0.6 dropped)
  -> 3. SpeakerVerifiedWhisperSTT (ERes2Net cosine >= SPEAKER_THRESHOLD; mute/unmute and fillers pass any voice)
  -> 4. JarvisIntentGate hold: a sentence ending in "..." waits HOLD_SECS = 2.0 for the rest
  -> 5. classify_fast_command: a lone "okay", "ok", "thanks", "thank you", "you", "bye" is dropped
  -> 6. ExecutiveFilter (typesafe/jev-1.14): ADDRESSED_TO_JARVIS or IGNORE_BACKGROUND
```

The log shows the result of each gate: `[Speaker] score=… pass|drop`, `holding for the rest`, `[User Transcript]`,
`Ignored self-talk`. It does not show which Whisper model, threshold or voiceprint was in use.

## The sessions behind the evidence

Each session ran a different setup, so scores and transcripts are only comparable within a row.

| Log date          | Run as                  | Mic        | Whisper        | Threshold      | Voiceprint  |
| :---------------- | :---------------------- | :--------- | :------------- | :------------- | :---------- |
| 11-03 20:47–22:25 | `uv run jarvis`         | LocalAudio | turbo (likely) | 0.5, then 0.35 | old         |
| 11-04 08:20       | `uv run jarvis connect` | LocalAudio | turbo (likely) | 0.35           | old         |
| 11-04 19:28–19:35 | `uv run jarvis`         | LocalAudio | turbo          | 0.35           | old         |
| 11-04 22:04–22:28 | `uv run jarvis`         | macOS VP   | turbo          | 0.35           | 11-04 22:18 |
| 11-06 16:30–16:44 | `jarvis connect`        | macOS VP   | distil         | 0.4            | 11-05 21:06 |

Notes: "macOS VP" is the macOS voice processing transport (echo cancelled); "LocalAudio" has no echo cancellation.
"turbo" is `mlx-community/whisper-large-v3-turbo`, "distil" is `mlx-community/distil-whisper-large-v3`. The model is
not logged; the checkout `.env` has had turbo since its weights were downloaded on 11-03 09:22, and the 11-04
worktree's `.env` still holds turbo and 0.35. The 11-03 threshold is inferred from the log: `hello` scored 0.43 and
was dropped at 20:57, `Ken sorry` scored 0.39 and passed at 21:48. The 11-05 voiceprint is the head-turn enrollment
from `b74d27b`; the 11-06 run is the only one on it, with 3 passes (0.44–0.50) and 1 drop (0.23).

## Findings

### 1. The installed Jarvis runs other settings than the checkout

`jarvis connect` reads `~/.config/jarvis/.env`; `uv run jarvis` reads the checkout's `.env` (`config.py`,
`JARVIS_HOME`). They differ:

| Setting             | `~/.config/jarvis/.env` (installed)     | Checkout `.env`                        |
| :------------------ | :-------------------------------------- | :------------------------------------- |
| `WHISPER_MODEL`     | `mlx-community/distil-whisper-large-v3` | `mlx-community/whisper-large-v3-turbo` |
| `SPEAKER_THRESHOLD` | `0.4`                                   | `0.35`                                 |

What the user tuned in the checkout (threshold 0.35 after reading the 11-03 scores) never reached the installed
Jarvis. Whichever values win, both files should hold them, and the startup log should name the model, the
threshold and the voiceprint file so the next investigation does not have to infer them.

Both models garble names. The log lines below are all the user's voice (score 0.44–0.72):

- turbo, 11-03 21:14:58: `the tree to continue walking.` ("T3 to continue working"); 21:15:07 `the Hustle Vlog`
  and 21:15:20 `the hook and hustle block` ("Hugger Hustle"); 22:25:26 `Hannaasal platform`.
- turbo, 11-04 19:32:39: `the neural particle`, 22:25:53 `the statute of happy voxel`.
- distil, 11-06 16:42:42: `You shoot up that to me.`; 16:43:07 `The tree is walking under the...`.

Distil is English-only and turbo is multilingual, which should favor turbo on a non-native accent, but the three
distil sentences are too few to show it. On synthetic speech (test below) both run at 0.63–0.64 s warm; cold, the
first call takes 1.4 s (distil) or 0.9 s (turbo).

### 2. The speech check drops answers to Jarvis's own questions

Every filter miss in the logs follows the same shape: Jarvis asks something, the user answers, Whisper garbles or
shortens the answer, and the decision model marks it `IGNORE_BACKGROUND` ("thinks out loud, or says only filler
words").

- 11-03 21:14:55: user `Open a new section on...` -> Jarvis "Which project should the new section go in — the
  platform one in Opscom, or somewhere else?" -> user `the tree to continue walking.` (0.51 pass) -> ignored.
- 11-03 21:47:52: Jarvis "T3 Code is still waiting to hear which page or thread you meant by 'new section.'" ->
  user `Ken sorry` (0.39 pass) -> ignored.
- 11-06 16:42:51: Jarvis "Sorry, Master — shoot WHAT up to you? The status of the Genes fix, or something else?"
  -> user `Yes, you should have got me how the... The tree is walking under the...` (0.44, 0.46 pass) -> ignored.
- Same pattern on 11-02 (older pipeline): `No.` and `now.` ignored at 15:17.

The decision model gets `jarvis_last_said` only when an agent asked a question or needs approval:
`JarvisIntentGate._gate` reads `state.active_agent_context`, which `notify_topic` fills for non-report topics
and clears when the turn ends. A clarifying question from the conversation model, or the "Go ahead?" at the end
of a retold plan, leaves it empty, so the answer is judged without context.

Not a filter miss: `somewhere else` at 21:15:03 (0.11) and `Let me check the current status of that somewhere
else.` on 11-04 08:20:23 (0.01) are Jarvis's own voice through the speakers, dropped by the voice check as they
should be; both predate the macOS voice processing transport (`docs/research/echo-cancellation.md`).

### 3. Short phrases score low on the voice check, and padding is not why

Owner sentences of two or more seconds score 0.44–0.72. Short ones sit near or under the threshold:

| Log            | Transcript            | Score | Threshold then | Result |
| :------------- | :-------------------- | :---- | :------------- | :----- |
| 11-03 20:48:46 | `Hello?`              | 0.26  | 0.5            | drop   |
| 11-03 20:57:12 | `hello`               | 0.43  | 0.5            | drop   |
| 11-03 20:57:23 | `hello`               | 0.34  | 0.5            | drop   |
| 11-03 20:57:27 | `Are you here?`       | 0.60  | 0.5            | pass   |
| 11-03 21:48:10 | `Ken sorry`           | 0.39  | 0.35           | pass   |
| 11-04 22:27:56 | `Stop it!`            | 0.38  | 0.35           | pass   |
| 11-06 16:43:31 | `What's he going on?` | 0.23  | 0.4            | drop   |

The first guess was dilution: `SegmentedSTTService` hands `run_stt` up to 1.0 s of pre-roll, the speech, the
0.6 s `stop_secs` tail and 0.5 s of digital zeros, so a 0.7 s "Hello?" arrives in a 2.8 s buffer that is 75%
non-speech, and `SpeakerEmbedder.embed` scores all of it. The synthetic test below says otherwise: removing the
zeros changed nothing (0.52 -> 0.51), and trimming to speech lowered the score of short phrases (0.52 -> 0.32,
0.77 -> 0.51) while leaving a 5 s sentence alone (0.88 -> 0.87). Short clips simply give the embedding too little
to work with; `docs/research/speaker-verification.md` quotes the same effect from the literature (EER 3.2% on full
clips, 13.2% at 1 s).

Lowering the threshold further is not safe either. Other voices reached 0.35 twice on 11-03 (`Taiwan.`, `Then,
then, I'm new.`) and a TV or video reached 0.30 on 11-04 22:26 (`I'm very happy to see you.`). At 0.35 the margin
is already about 0.05; at 0.4 it drops owner phrases like `Stop it!` (0.38) and `Ken sorry` (0.39).

### 4. The trailing-off hold can chain

`JarvisIntentGate` holds a sentence ending in `...`, `…`, `-` or `—` for 2.0 s and joins the next segment to it.
If the joined text also ends that way it is held again. Added in `28920ff` (11-06 00:54); one chain so far:

- 11-06 16:43:04.565 `Yes, you should have got me how the...` held; 16:43:07.320 joined with `The tree is
walking under the...` and held again; released 16:43:09.322; ignored by the filter at 16:43:09.563.

The first hold cost nothing (the user was still talking); the second added 2.0 s to a sentence that was then
dropped anyway. Total: 4.8 s from the first transcript to the decision. `...` endings happen with both models
(`Open a new section on...` under turbo on 11-03).

### 5. The filler list is deliberate and still needed

`classify_fast_command` drops a lone "okay", "ok", "thanks", "thank you", "you" or "bye" before the decision model
runs (`_WHISPER_FILLERS`, `state.py`, added in `d78d11f` on 11-03 16:19). The code comment gives the reason: Whisper
writes these on noise and silence, and noise must not answer "okay" to an approval. The synthetic test confirms the
need: on 2.8 s of noise or zeros both models return `Thank you.` with `no_speech_prob` between 0.00 and 0.13, so
Pipecat's 0.6 cutoff never catches it.

The cost: "Okay." after a retold plan is dropped; "yes" or "go ahead" works. No logged miss comes from this list
yet. The `Okay.` ignored on 11-02 15:16:25 predates it and was the decision model's call. Log-reading trap: a filler
is logged as `[Speaker] … pass` (it takes the fast-command path in `SpeakerVerifiedWhisperSTT`) and then vanishes
with only a debug `Ignored self-talk` line.

### 6. A vocabulary prompt does not fix names; the first sentence pays for model loading

Pipecat's `WhisperSTTServiceMLX.run_stt` calls `mlx_whisper.transcribe` without `initial_prompt` (mlx-whisper 0.4.3
supports it). Tested below with "Jarvis, T3 Code, Claude Code, OpenCode, Hermes, Hugger Hustle, Happy Voxel,
OpsCom, Neuroparticles." as the prompt: on distil it changed nothing useful; on turbo it leaked prompt words into a
sentence that never said them (`OpenCode, OpsCom, Project and Tasker East for a status update.`) and turned
`hello` into `mop mop mop`. On noise it made `!` and `You` into `Thank you.`. Rejected.

`WhisperSTTServiceMLX._load` is a no-op; the weights load on the first `run_stt`. The first sentence after startup
waits 0.3–0.7 s longer than the rest (1.4 s vs 0.63 s for distil, 0.9 s vs 0.64 s for turbo).

### 7. A slow reply reads as not being heard

On 11-06 the one answered sentence took 8.8 s from transcript to first spoken word:

| Step                                     | Time         | Took  |
| :--------------------------------------- | :----------- | :---- |
| `[User Transcript]` `You shoot up that…` | 16:42:42.870 |       |
| Mem0 lookup starts                       | 16:42:43.376 | 0.5 s |
| LLM request (after memory)               | 16:42:45.326 | 2.0 s |
| First TTS sentence                       | 16:42:51.694 | 6.4 s |

On 11-04, before `6d16bda` turned executive thinking back on, the LLM step took 0.5–1.4 s and a whole first turn
3.1 s (of which 1.6 s was the first, cold, memory lookup). One 11-06 sample is not proof that thinking costs 5 s,
and OpenRouter provider latency varies, but 8.8 s of silence is long enough that a user repeats the sentence, and
the repeat then arrives as a new sentence while the first answer is still being spoken.

## Local test (synthetic speech, 2026-11-06)

Throwaway script `stt_exp.py` in the session scratchpad; macOS `say` voice "Albert" as the owner, "Bad News" as
the other voice, each clip padded like Pipecat does (1.0 s white noise at -50 dBFS, speech, 0.6 s noise, 0.5 s
zeros), speaker model `3dspeaker_speech_eres2net_sv_en_voxceleb_16k`, voiceprint from the five `enroll.py` lines
in 4 s windows. A synthetic voice is deterministic and not the user's mic, so the numbers show mechanics, not the
real-world scores.

| Clip (speech length)      | Padded as sent | Without zeros | Trimmed to speech | Trimmed, voiceprint trimmed |
| :------------------------ | :------------- | :------------ | :---------------- | :-------------------------- |
| `Hello?` (0.74 s)         | 0.52           | 0.51          | 0.32              | 0.28                        |
| `Stop it!` (1.2 s)        | 0.77           | 0.77          | 0.51              | 0.50                        |
| 5 s sentence              | 0.88           | 0.89          | 0.87              | 0.89                        |
| Other voice, 5 s sentence | 0.20           |               | 0.23              |                             |

Whisper on the same clips: both models warm in 0.63–0.64 s; `T3` came out as `Telltie free` (distil) and `D3`
(turbo), `Hugger` as `Hucker` and `hacker`, with and without the vocabulary prompt.

## Proposals

Smallest first. Each: what breaks, what to change, how to check.

1. **Nothing in the log says which setup ran.** Log `WHISPER_MODEL`, `SPEAKER_THRESHOLD` and the voiceprint path
   once at startup in `main.py`. Check: the next log names them.
2. **Installed and checkout settings differ.** Pick one pair (turbo + 0.35 is what the 11-03 scores were read
   for) and put it in `~/.config/jarvis/.env`, `config.py` and `defaults/env.example`. Check: `jarvis connect` and
   `uv run jarvis` log the same values.
3. **Answers to Jarvis's own questions are judged without context.** In `JarvisIntentGate._gate`, fall back from
   the agent question to the last assistant message in the `LLMContext` (`context.messages`) as
   `recent_jarvis_question`. `scripts/bench_intent.py` only calls `classify_user_utterance(text)`, so add cases
   with a `jarvis_last_said` column first (garbled and one-word answers after a question, and background talk
   right after Jarvis speaks), then run the benchmark before and after as the rule requires. Risk: more background
   sentences accepted in the seconds after Jarvis speaks; the new cases measure that.
4. **A joined sentence is held again.** In `JarvisIntentGate.process_frame`, hold only when nothing was joined.
   Cost: a sentence with two long pauses is split in two. Check: unit test on two `...` segments in a row.
5. **Reply time is not measured.** Before deciding on executive thinking, log the time from `[User Transcript]`
   to the first TTS sentence for a few sessions. If it stays above about 4 s, revisit the "thinking always on"
   rule in `AGENTS.md` with those numbers; the decision is Sang's.
6. Optional: run one `mlx_whisper.transcribe` on a second of zeros at startup so the first sentence is not the
   slow one (0.3–0.7 s once per start).

Tested and rejected: a Whisper `initial_prompt` (finding 6); trimming silence before the speaker embedding
(finding 3); a threshold under 0.35 (finding 3); removing the filler list (finding 5).

Untested idea: enroll with short phrases too ("Hello?", "Yes.", "Stop it."), so the voiceprint covers how short
clips embed. The synthetic test did not try it; a deterministic voice could not show whether it helps a real one.

## Status (2026-11-06, evening)

Done, uncommitted on `fix/voice-recognition-misses`: proposals 1, 2, 4 and 6; proposal 3 with one sentence added to
`INTENT_QUESTION` that tells the decision model what `jarvis_last_said` means. Jarvis's own last reply counts for
`REPLY_WINDOW_SECS = 20` after it stops talking; an open agent question still comes first. Proposal 5 needs no code:
`[User Transcript]` and `Generating TTS` lines already carry the timestamps.

`scripts/bench_intent.py` now has eight `REPLY_CASES` (five answers from the logs, three room lines right after
Jarvis spoke). Results at 3 rounds:

| Run                                         | Accuracy | Misses                                       |
| :------------------------------------------ | :------- | :------------------------------------------- |
| Before, old 45 cases                        | 98%      | the game line                                |
| Context passed, prompt unchanged            | 92%      | the game line, three garbled answers         |
| Context passed, `jarvis_last_said` sentence | 94%      | the game line, `Ken sorry`, the 11-06 answer |

The old cases score the same in every run, and all three room lines stay ignored. The 94% is below the 97% bar
only because of the new cases, so keeping them needs Sang's OK. A second wording ("any reply, however garbled")
flipped `Uh, yeah, okay` and `um yeah okay` to heard and was reverted.
