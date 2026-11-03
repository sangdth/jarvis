"""Owner voice check: sherpa-onnx speaker embeddings (3D-Speaker ERes2Net, English, VoxCeleb).

Research and model choice: docs/research/speaker-verification.md.
"""

from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

import numpy as np
from loguru import logger

SAMPLE_RATE = 16000
MODEL_NAME = "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{MODEL_NAME}"
MODEL_SHA256 = "c59158379255ad66e161679cca6af8d52d51e389e3224ab7d7a7baae295c2db5"
JARVIS_DIR = Path("~/.jarvis").expanduser()
MODEL_PATH = JARVIS_DIR / "models" / MODEL_NAME
VOICEPRINT_PATH = JARVIS_DIR / "owner_voice.npy"


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
        stream.accept_waveform(SAMPLE_RATE, np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0)
        stream.input_finished()
        if not self._extractor.is_ready(stream):
            return None
        return np.array(self._extractor.compute(stream), dtype=np.float32)


def cosine(a: np.ndarray | None, b: np.ndarray) -> float:
    """Cosine similarity; 0.0 for missing or broken embeddings (very short silence gives NaN)."""
    if a is None:
        return 0.0
    score = float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
    return score if np.isfinite(score) else 0.0


def save_voiceprint(embeddings: list[np.ndarray], path: Path = VOICEPRINT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, np.mean(embeddings, axis=0))
    return path


def load_voiceprint(path: Path = VOICEPRINT_PATH) -> np.ndarray:
    if not path.exists():
        raise FileNotFoundError(f"No owner voiceprint at {path}. Run `uv run jarvis-enroll` first.")
    return np.load(path)
