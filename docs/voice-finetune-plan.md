# Fine-tuning Whisper on the owner's voice (plan, 2026-11-08)

Status: round 1 trained and in use in the dev checkout (2026-11-08). Base model and scope decided by Sang:
`whisper-large-v3`, English only.

## Round 1 results

293 clips (15.6 min), 252 train / 41 held out. LoRA r=32 on all attention projections, lr 5e-4, 5 epochs (160
updates at ~27 s, 72 min, 9.7 GB peak on an M1 Pro). The base conversion matched `mlx-community/whisper-large-v3-mlx`
exactly (20.4%), so `save_mlx` is correct.

| Model                 | Owner, held out | General English (4 synthetic voices) | Median   |
| :-------------------- | :-------------- | :----------------------------------- | :------- |
| large-v3-turbo        | 26.9%           | 1.6%                                 | 645 ms   |
| large-v3              | 20.4%           | 1.6%                                 | 939 ms   |
| large-v3, owner LoRA  | 16.9%           | 1.6%                                 | 870 ms   |

Training loss reached ~0: the model memorized the training sentences. "Merge the PR for me" is still "Mode the
PR". Next round: hold a validation split out of train to pick the epoch count (the test split stays untouched),
fewer epochs or a lower learning rate, and more recordings of the words still missed.

## Why

Stock Whisper mishears the owner's English: "merge the PR" came out as "mark the BR", "Murgs the beer" and
"March the PR" (log of 2026-11-08). A domain prompt or a bigger model only tilt the odds; fine-tuning teaches the
model how this speaker says these words. On synthetic speech, large-v3 already beat turbo on rarer words
("eigenvalues", "Hugger Hustle") and invented nothing from silence, at about +260 ms per sentence.

## Steps

1. **Record a labeled voice set.** `uv run python scripts/voice/record.py` reads `scripts/voice/sentences.txt`
   (293 lines: git and PR commands, coding, science and ML, project and agent names, Jarvis commands) through the
   same macOS voice processing Jarvis listens through. Clips and `manifest.jsonl` go to `JARVIS_HOME/voice_data`
   (0700 / 0600, gitignored). One sentence in seven is held out by its text (`split_of`), 41 of 293 today.
2. **Train a LoRA on `openai/whisper-large-v3`** with PyTorch on the Mac's GPU (MPS, float32): adapters on the
   attention projections, a low learning rate, and about 20% general English speech mixed in so the model keeps
   words outside the set. The script declares its own dependencies (PEP 723), so torch and transformers never
   become Jarvis dependencies.
3. **Merge and convert to MLX** as for `sangdth/phowhisper-large-vien-cs-mlx-16bit`: merge the adapter
   (W + alpha / r · B · A), convert with mlx-examples `whisper/convert.py`, rename to `weights.safetensors`. The
   model stays a local folder in `JARVIS_HOME/models/`; it is tuned to one voice and is not uploaded.
4. **Measure word error rate** on the held-out sentences for base turbo, base large-v3 and the tuned model, plus a
   general-English set to catch what got worse. The tuned model ships only if it beats both on the owner's voice.
5. **Ship**: `WHISPER_MODEL` points at the folder; README and AGENTS.md describe record, train, evaluate.

## Risks

- Read sentences are cleaner than real commands. Real clips from `SAVE_SEGMENTS`, with corrected text, are the
  second round of data.
- The model may get worse at other voices; Jarvis acts only on the owner's voice, and step 4 measures it.
- English only: `LANGUAGE=vi` keeps PhoWhisper.
- Training time on an M1 Pro is not measured yet; large-v3 is 1.55B parameters in float32 (about 6 GB of weights).
