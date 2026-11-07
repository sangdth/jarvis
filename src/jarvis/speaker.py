"""Owner voice check: sherpa-onnx speaker embeddings (3D-Speaker ERes2Net, English, VoxCeleb).

Research and model choice: docs/research/speaker-verification.md.
"""

from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

import numpy as np
from loguru import logger

from jarvis.config import JARVIS_HOME

SAMPLE_RATE = 16000
MODEL_NAME = "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{MODEL_NAME}"
MODEL_SHA256 = "c59158379255ad66e161679cca6af8d52d51e389e3224ab7d7a7baae295c2db5"
MODEL_PATH = JARVIS_HOME / "models" / MODEL_NAME
VOICEPRINT_PATH = JARVIS_HOME / "owner_voice.npy"
FRAME = SAMPLE_RATE // 50  # 20 ms


def ensure_model(path: Path = MODEL_PATH) -> Path:
    """Download the model once; a file with the wrong checksum is never loaded."""
    if path.exists() and _sha256(path) == MODEL_SHA256:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    logger.info(f"Downloading speaker model to {path}")
    urllib.request.urlretrieve(MODEL_URL, tmp)
    if _sha256(tmp) != MODEL_SHA256:
        tmp.unlink()
        raise RuntimeError(f"Speaker model download failed its checksum: {MODEL_URL}")
    tmp.replace(path)
    return path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class SpeakerEmbedder:
    def __init__(self, model_path: Path | None = None) -> None:
        import sherpa_onnx

        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model_path or ensure_model()), num_threads=2)
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)

    def embed(self, pcm16: bytes) -> np.ndarray | None:
        """Embedding of 16 kHz mono 16-bit PCM; None when the audio is too short to score."""
        stream = self._extractor.create_stream()
        samples = trim_silence(np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0)
        stream.accept_waveform(SAMPLE_RATE, samples)
        stream.input_finished()
        if not self._extractor.is_ready(stream):
            return None
        return np.array(self._extractor.compute(stream), dtype=np.float32)


def trim_silence(samples: np.ndarray) -> np.ndarray:
    """Drop the quiet start and end: the pause before and after a sentence blurs the speaker embedding."""
    frames = len(samples) // FRAME
    if frames == 0:
        return samples
    rms = np.sqrt(np.mean(samples[: frames * FRAME].reshape(frames, FRAME) ** 2, axis=1))
    loud = np.flatnonzero(rms >= rms.max() * 0.1)  # within 20 dB of the loudest frame
    pad = 5  # 100 ms each side keeps the soft start and end of words
    return samples[max(loud[0] - pad, 0) * FRAME : min(loud[-1] + 1 + pad, frames) * FRAME]


def cosine(a: np.ndarray | None, b: np.ndarray) -> float:
    """Cosine similarity; 0.0 for missing or broken embeddings (very short silence gives NaN)."""
    if a is None:
        return 0.0
    score = float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
    return score if np.isfinite(score) else 0.0


def save_voiceprint(embeddings: list[np.ndarray], path: Path = VOICEPRINT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Each clip scaled to length 1 first, so one loud or odd clip doesn't pull the average toward itself
    np.save(path, np.mean([e / np.linalg.norm(e) for e in embeddings], axis=0))
    return path


def load_voiceprint(path: Path = VOICEPRINT_PATH) -> np.ndarray:
    if not path.exists():
        raise FileNotFoundError(f"No owner voiceprint at {path}. Run `jarvis enroll` first.")
    return np.load(path)
