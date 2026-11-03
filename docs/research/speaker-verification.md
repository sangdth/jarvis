# Speaker verification with sherpa-onnx (research, 2026-11-03)

Goal: Jarvis acts only on the owner's voice. Built as `SpeakerVerifiedWhisperSTT` (`src/jarvis/voice_gates.py`).

## Library

- `sherpa-onnx` 2.0.1 on PyPI (2026-10-26), Apache 2.0, macOS arm64 wheels for cp310–cp315. No torch.
- API: `SpeakerEmbeddingExtractor(config)` → `create_stream()` → `accept_waveform(16000, float32)` →
  `input_finished()` → `compute()` gives a numpy vector. `SpeakerEmbeddingManager(dim)` has `add(name, embs)`
  (averages several clips), `verify(name, emb, threshold)` and `search(emb, threshold)`, all cosine.
  Input is 16 kHz mono, which matches the Pipecat pipeline.
- Models: release `speaker-recongition-models` on k2-fsa/sherpa-onnx (the tag is misspelled upstream).

## Published accuracy (VoxCeleb1-O EER, full-length clips)

| Model (sherpa-onnx file)                          | Params | EER   | Size  |
| :------------------------------------------------ | :----- | :---- | :---- |
| `wespeaker_en_voxceleb_resnet34_LM`               | 6.6M   | 0.80% | 25 MB |
| `3dspeaker_speech_eres2net_sv_en_voxceleb_16k`    | 6.6M   | 0.84% | 25 MB |
| `wespeaker_en_voxceleb_resnet293_LM`              | large  | 0.53% | 109 MB |

Notes: the WeSpeaker numbers come from `examples/voxceleb/v2/README.md`, without score normalization. The
ERes2Net number is for the base-lm row in 3D-Speaker's `egs/voxceleb/sv-eres2net`. Both were trained on
VoxCeleb2-dev. Skip the `zh-cn` and `cnceleb` files, which were trained on Chinese speech.

Short clips matter more than the model. Published Thin-ResNet34 results go from 3.2% EER on full clips to 7.7%
at 2 s and 13.2% at 1 s (arXiv 2005.03329). Many Jarvis commands are 1–2 s long.

## Local test on this Mac (`/tmp/spk-bench/bench2.py`, throwaway)

Each voice was enrolled from two macOS `say` sentences (averaged) and tested on an unseen sentence. Four voices
were used. "Gap" is the lowest same-voice score minus the highest different-voice score; a positive gap means a
threshold exists. 2 threads, warm.

| Model                | Dim | Embed  | Gap 1.3 s | Gap 3.4 s |
| :------------------- | :-- | :----- | :-------- | :-------- |
| 3D-Speaker ERes2Net  | 192 | ~100 ms | +0.22    | +0.53     |
| NeMo TitaNet-small   | 192 | ~33 ms | +0.25     | +0.34     |
| WeSpeaker ResNet34-LM | 256 | ~120 ms | +0.13   | +0.23     |
| 3D-Speaker CAM++ (en) | 512 | ~40 ms | 0.00     | -0.12     |

Notes: these are synthetic voices, so the gaps rank the models; they don't give a threshold. Two `say` voices
(Eddy, Reed) share one synth engine, so every model scored them as the same person, and they were dropped. The
CAM++ result looks like a bad export or a feature mismatch; don't use it without finding out why.

## Takeaways for Jarvis

- Pick ERes2Net (en, VoxCeleb). It gave the widest gap on short clips and has the same size and speed as ResNet34.
  ResNet34-LM puts all scores high (0.6–0.95), so its threshold has a thin margin. TitaNet-small is the backup
  if speed ever matters.
- About 100 ms per sentence. If it runs alongside Whisper after VAD stop, it adds almost no delay.
- Commands under about 1 s ("stop", "shut up") will be the weak spot. Decide whether mute and stop phrases skip
  the gate. The risk is small: anyone could mute Jarvis.
- Calibrate on real recordings: about 20 owner sentences plus TV, podcasts and other people in the room, using
  the actual mic. Set the threshold from that, not from the 0.5–0.6 used in upstream examples.
- Licenses: sherpa-onnx is Apache 2.0. WeSpeaker says its VoxCeleb weights follow the dataset's CC BY 4.0. The
  3D-Speaker ModelScope license was not confirmed (the model page didn't load); check it before shipping.
