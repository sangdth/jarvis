# /// script
# requires-python = ">=3.12"
# dependencies = ["torch>=2.5", "transformers>=4.46", "peft>=0.13", "mlx>=0.20", "numpy"]
# ///
"""Fine-tune Whisper large-v3 on the owner's voice (a LoRA), then save it as an MLX model Jarvis can load.

Usage: uv run scripts/voice/train.py [--data DIR] [--out DIR] [--epochs N] [--max-steps N] [--no-train]
--data is the voice set from record.py (default: this checkout's voice_data), --out the MLX model folder (default:
models/whisper-large-v3-owner next to --data). --no-train converts the base model only, to check the conversion:
evaluate.py on it must match mlx-community/whisper-large-v3-mlx. Then point WHISPER_MODEL at --out.
Runs on the Mac's GPU (MPS) in float32; its dependencies stay out of Jarvis's own environment.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
import wave
from pathlib import Path

import mlx.core as mx
import numpy as np
import torch
from peft import LoraConfig, get_peft_model
from transformers import WhisperForConditionalGeneration, WhisperProcessor

BASE = "openai/whisper-large-v3"
ROOT = Path(__file__).resolve().parents[2]


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1, path
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def mlx_key(key: str) -> str:
    """A Hugging Face Whisper weight name as mlx-whisper names it (mlx-examples whisper/convert.py, hf_to_pt)."""
    for old, new in [
        ("model.", ""), (".layers", ".blocks"), (".self_attn", ".attn"), (".attn_layer_norm", ".attn_ln"),
        (".encoder_attn.", ".cross_attn."), (".encoder_attn_layer_norm", ".cross_attn_ln"),
        (".final_layer_norm", ".mlp_ln"), (".q_proj", ".query"), (".k_proj", ".key"), (".v_proj", ".value"),
        (".out_proj", ".out"), (".fc1", ".mlp1"), (".fc2", ".mlp2"), ("embed_positions.weight", "positional_embedding"),
        ("decoder.embed_tokens", "decoder.token_embedding"), ("encoder.layer_norm", "encoder.ln_post"),
        ("decoder.layer_norm", "decoder.ln"),
    ]:
        key = key.replace(old, new)
    return key


def save_mlx(model: WhisperForConditionalGeneration, out: Path) -> None:
    """Write the model as float16 MLX weights and the config mlx-whisper reads."""
    weights = {}
    for key, value in model.state_dict().items():
        key = mlx_key(key)
        # The output projection is the token embedding; mlx-whisper computes the encoder's sinusoidal positions.
        if key == "proj_out.weight" or key == "encoder.positional_embedding":
            continue
        value = value.detach().to("cpu", torch.float32)
        if "conv" in key and value.ndim == 3:
            value = value.permute(0, 2, 1)  # PyTorch conv1d is (out, in, k); MLX is (out, k, in)
        weights[key] = mx.array(value.contiguous().numpy()).astype(mx.float16)
    c = model.config
    config = {
        "n_mels": c.num_mel_bins, "n_audio_ctx": c.max_source_positions, "n_audio_state": c.d_model,
        "n_audio_head": c.encoder_attention_heads, "n_audio_layer": c.encoder_layers, "n_vocab": c.vocab_size,
        "n_text_ctx": c.max_target_positions, "n_text_state": c.d_model, "n_text_head": c.decoder_attention_heads,
        "n_text_layer": c.decoder_layers, "model_type": "whisper",
    }
    out.mkdir(parents=True, exist_ok=True)
    mx.save_safetensors(str(out / "weights.safetensors"), weights)
    (out / "config.json").write_text(json.dumps(config, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=ROOT / "voice_data")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--max-steps", type=int, default=0, help="stop after this many updates (a timing run)")
    parser.add_argument("--no-train", action="store_true")
    args = parser.parse_args()
    out = args.out or args.data.parent / "models" / "whisper-large-v3-owner"
    device = "mps" if torch.backends.mps.is_available() else "cpu"

    processor = WhisperProcessor.from_pretrained(BASE)
    processor.tokenizer.set_prefix_tokens(language="english", task="transcribe", predict_timestamps=False)
    model = WhisperForConditionalGeneration.from_pretrained(BASE, torch_dtype=torch.float32)

    if not args.no_train:
        rows = [json.loads(line) for line in (args.data / "manifest.jsonl").read_text().splitlines() if line.strip()]
        train = [r for r in rows if r["split"] == "train"]  # the test split is never seen here
        print(f"{len(train)} training clips, {args.epochs} epochs, on {device}")
        # Small adapters on every attention projection, encoder (how the voice sounds) and decoder (which words).
        model = get_peft_model(model, LoraConfig(r=32, lora_alpha=64, lora_dropout=0.05,
                                                 target_modules=["q_proj", "k_proj", "v_proj", "out_proj"]))
        model.print_trainable_parameters()
        model.to(device)
        model.base_model.model.config.use_cache = False
        model.base_model.model.gradient_checkpointing_enable({"use_reentrant": False})
        model.base_model.model.enable_input_require_grads()

        batch, accumulate = 2, 4  # 8 sentences per update
        updates = math.ceil(len(train) / (batch * accumulate)) * args.epochs
        if args.max_steps:
            updates = min(updates, args.max_steps)
        optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=5e-4, weight_decay=0.01)
        warmup = max(1, updates // 10)
        scheduler = torch.optim.lr_scheduler.LambdaLR(
            optimizer, lambda s: min(1.0, (s + 1) / warmup) * max(0.0, (updates - s) / max(1, updates - warmup)))
        start_token = model.base_model.model.config.decoder_start_token_id
        random.seed(0)
        step, started = 0, time.time()
        model.train()
        while step < updates:
            random.shuffle(train)
            for i in range(0, len(train), batch):
                chunk = train[i:i + batch]
                feats = processor.feature_extractor([load_wav(args.data / r["file"]) for r in chunk],
                                                    sampling_rate=16000, return_tensors="pt").input_features.to(device)
                # Whisper's own training text starts with a space after <|notimestamps|>.
                labels = processor.tokenizer([" " + r["text"] for r in chunk], return_tensors="pt", padding=True)
                ids = labels.input_ids.masked_fill(labels.attention_mask == 0, -100)
                if (ids[:, 0] == start_token).all():
                    ids = ids[:, 1:]  # the model adds the start token itself
                loss = model(input_features=feats, labels=ids.to(device)).loss / accumulate
                loss.backward()
                if (i // batch + 1) % accumulate == 0:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                    optimizer.step()
                    scheduler.step()
                    optimizer.zero_grad()
                    step += 1
                    elapsed = time.time() - started
                    print(f"update {step}/{updates} loss {loss.item() * accumulate:.3f} "
                          f"{elapsed / step:.1f} s/update, ~{elapsed / step * (updates - step) / 60:.0f} min left",
                          flush=True)
                    if step >= updates:
                        break
        model = model.merge_and_unload()  # W + (alpha / r) B A, so the result is a plain Whisper model

    save_mlx(model.to("cpu"), out)
    print(f"Saved MLX model to {out}. Check it: uv run python scripts/voice/evaluate.py {out}")


if __name__ == "__main__":
    main()
