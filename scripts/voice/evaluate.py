"""Word error rate of Whisper models on the owner's held-out voice clips (scripts/voice/record.py).

Usage: uv run python scripts/voice/evaluate.py [model ...] [--split test|train|all] [--data DIR]
A model is an MLX Hugging Face repo or a local MLX folder; with none, base turbo and base large-v3. The call is the
one Jarvis makes (pipecat's WhisperSTTServiceMLX): temperature 0, English, segments under no_speech_prob 0.6.
"""

from __future__ import annotations

import json
import re
import sys
import time
import wave
from pathlib import Path

import mlx_whisper
import numpy as np

from jarvis.config import JARVIS_HOME

DATA_DIR = JARVIS_HOME / "voice_data"
BASE_MODELS = ["mlx-community/whisper-large-v3-turbo", "mlx-community/whisper-large-v3-mlx"]


def words(text: str) -> list[str]:
    """Lowercase words without punctuation: "PyTest," and "pytest" count as the same word."""
    return re.sub(r"[^\w\s']", " ", text.lower().replace("-", " ")).split()


def word_errors(ref: list[str], hyp: list[str]) -> int:
    """Word-level edit distance: substitutions, deletions and insertions."""
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[-1]


def load(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(audio: np.ndarray, model: str) -> str:
    out = mlx_whisper.transcribe(audio, path_or_hf_repo=model, temperature=0.0, language="en")
    return " ".join(s["text"].strip() for s in out.get("segments", []) if s.get("no_speech_prob", 0.0) < 0.6)


def main() -> None:
    args = sys.argv[1:]
    split = args[args.index("--split") + 1] if "--split" in args else "test"
    data = Path(args[args.index("--data") + 1]) if "--data" in args else DATA_DIR  # any folder with a manifest.jsonl
    models = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] not in ("--split", "--data"))]
    rows = [json.loads(line) for line in (data / "manifest.jsonl").read_text().splitlines() if line.strip()]
    rows = [r for r in rows if split == "all" or r["split"] == split]
    clips = [(r["text"], load(data / r["file"])) for r in rows]
    print(f"{len(clips)} {split} clips from {data}")
    for model in models or BASE_MODELS:
        transcribe(np.zeros(16000, dtype=np.float32), model)  # load the weights before timing
        errors = total = 0
        times, misses = [], []
        for text, audio in clips:
            start = time.perf_counter()
            heard = transcribe(audio, model)
            times.append((time.perf_counter() - start) * 1000)
            n = word_errors(words(text), words(heard))
            errors, total = errors + n, total + len(words(text))
            if n:
                misses.append((text, heard))
        print(f"\n{model}: WER {errors / total:.1%} ({errors}/{total} words), "
              f"{len(misses)}/{len(clips)} sentences wrong, median {np.median(times):.0f} ms")
        for text, heard in misses:
            print(f"  said {text!r}\n  hrd  {heard!r}")


if __name__ == "__main__":
    main()
