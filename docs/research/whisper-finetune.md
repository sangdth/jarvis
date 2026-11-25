# Fine-Tuning Whisper on the Owner's Voice (Research, 2026-11-08)

Round 1 trained on `openai/whisper-large-v3` (English only; `LANGUAGE=vi` keeps PhoWhisper) and tested in the dev
checkout. Stock Whisper mishears domain and git phrases ("merge the PR" -> "mark the BR", "Murgs the beer", "March the
PR").

## Round 1 Results

293 clips (15.6 min), split deterministically by text (`split_of`, 1 in 7 held out): 252 train / 41 test. LoRA `r=32`
on attention projections (`q/k/v/out_proj`), `lr=5e-4`, 5 epochs (160 updates at ~27 s/update, 72 min total, 9.7 GB peak
on M1 Pro MPS, float32). Base conversion matched `mlx-community/whisper-large-v3-mlx` exactly (20.4%), confirming
`save_mlx`.

| Model                | Owner, held out | General English (4 synth voices) | Median |
| :------------------- | :-------------- | :------------------------------- | :----- |
| large-v3-turbo       | 26.9%           | 1.6%                             | 645 ms |
| large-v3             | 20.4%           | 1.6%                             | 939 ms |
| large-v3, owner LoRA | 16.9%           | 1.6%                             | 870 ms |

Training loss hit `~0`: the adapter memorized the training sentences ("Merge the PR for me" still transcribed as "Mode
the PR"). Round 2 needs a validation split carved out of `train` for epoch early-stopping (leaving `test` untouched),
lower learning rate or fewer epochs, and real corrected clips from `SAVE_SEGMENTS` (tracked in
`docs/self-improvement.md` §3).

## Scripts (`scripts/voice/`)

All three scripts keep PyTorch/PEFT/Transformers out of Jarvis's `.venv` (via PEP 723 inline dependencies in
`train.py`). Clips live in `JARVIS_HOME/voice_data` (`0700`/`0600`, gitignored); converted models stay local in
`JARVIS_HOME/models/` and are never uploaded.

```bash
# 1. Record sentences through macOS voice processing (1 in 7 held out for test)
uv run python scripts/voice/record.py

# 2. Train LoRA on MPS and export float16 MLX weights (no `python`: uv reads train.py's PEP 723 header)
uv run scripts/voice/train.py --data "${JARVIS_HOME:-.}/voice_data" --out "${JARVIS_HOME:-.}/models/whisper-large-v3-owner"

# 3. Measure WER on the held-out test split (set WHISPER_MODEL=<out> only if it beats base)
uv run python scripts/voice/evaluate.py "${JARVIS_HOME:-.}/models/whisper-large-v3-owner" --split test
```

## Notes and Limits

- Read sentences are cleaner than spontaneous commands; real clips from `SAVE_SEGMENTS` (reviewed before training) are
  the intended Round 2 data source.
- Fine-tuning for one voice can hurt other voices; always gate on both held-out owner WER and general-English WER.
- English only: `LANGUAGE=vi` uses `sangdth/phowhisper-large-vien-cs-mlx-16bit`.
