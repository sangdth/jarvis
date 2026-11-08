"""Owner voice check: sherpa-onnx speaker embeddings (3D-Speaker ERes2Net, English, VoxCeleb).

Research and model choice: docs/research/speaker-verification.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
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
SPEAKERS_DIR = JARVIS_HOME / "speakers"
MAX_PROFILE_CLIPS = 10
FRAME = SAMPLE_RATE // 50  # 20 ms

# Words that follow "I'm ..." or "this is ..." in normal speech and are never a person's name.
_NON_NAMES = {
    "a", "an", "the", "my", "your", "his", "her", "our", "their", "this", "that", "here", "there", "back",
    "tired", "good", "fine", "great", "okay", "ok", "well", "ready", "busy", "done", "free", "home", "late",
    "early", "going", "working", "talking", "listening", "looking", "thinking", "trying", "asking", "saying",
    "doing", "having", "feeling", "just", "still", "also", "very", "so", "too", "not", "no", "yes", "sure",
    "sorry", "happy", "glad", "excited", "new", "in", "on", "at", "from", "with", "for", "about", "someone",
    "somebody", "anyone", "friend", "brother", "sister", "wife", "husband", "partner", "colleague", "coworker",
    "guest", "mom", "mother", "dad", "father", "son", "daughter", "jarvis", "javis", "bạn", "anh", "em", "chị",
    "mình", "tôi", "con", "người", "ai", "đây", "đó",
}

_EXPLICIT_NAME_RE = re.compile(
    r"\b(?:my\s+name\s+is|my\s+name's|you\s+can\s+call\s+me|call\s+me|tên\s+(?:mình|tôi|anh|em|chị)\s+là)\s+"
    r"([A-Za-zÀ-ỹ][\wÀ-ỹ'-]*(?:\s+[A-Za-zÀ-ỹ][\wÀ-ỹ'-]*)?)",
    re.IGNORECASE,
)
_SELF_INTRO_CS_RE = re.compile(
    r"\b(?:[Ii]'m|[Ii]\s+am|[Tt]his\s+is|[Ii]t's|[Mm]ình\s+là|[Tt]ôi\s+là|[Aa]nh\s+là|[Ee]m\s+là|[Cc]hị\s+là)\s+"
    r"([A-ZÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ][\wÀ-ỹ'-]*)"
)
_SELF_INTRO_CI_RE = re.compile(
    r"\b(?:i'm|i\s+am|this\s+is|it's|mình\s+là|tôi\s+là|anh\s+là|em\s+là|chị\s+là)\s+"
    r"([A-Za-zÀ-ỹ][\wÀ-ỹ'-]*)",
    re.IGNORECASE,
)
_INTRODUCED_NAME_RE = re.compile(
    r"\b(?:this\s+is|meet|say\s+(?:hi|hello)\s+to|introduce(?:\s+you\s+to)?|đây\s+là|làm\s+quen\s+với|giới\s+thiệu(?:\s+với\s+\w+)?)"
    r"(?:\s+(?:my|our|a|the)?\s*(?:good\s+|best\s+|new\s+|close\s+)?(?:friend|brother|sister|wife|husband|partner|colleague|coworker|guest|son|daughter|bạn|anh|em|chị)?)*"
    r"\s*,?\s*([A-ZÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ][\wÀ-ỹ'-]*)"
)


def _clean_name(raw: str) -> str | None:
    words = [w.strip(".,!?;:\"'()[]{}") for w in raw.split()]
    words = [w for w in words if w and w.lower() not in _NON_NAMES and not w.isdigit()]
    if not words:
        return None
    name = words[0]
    return name if name[0].isupper() else name.title()


def extract_speaker_name(transcript: str, *, allow_bare: bool = False) -> str | None:
    """Extract a speaker's name from 'I'm David' / 'My name is David' (or a bare name reply when asked)."""
    cleaned = transcript.strip()
    if not cleaned:
        return None
    if m := _EXPLICIT_NAME_RE.search(cleaned):
        return _clean_name(m.group(1))
    if m := _SELF_INTRO_CS_RE.search(cleaned):
        return _clean_name(m.group(1))
    if allow_bare:
        if m := _SELF_INTRO_CI_RE.search(cleaned):
            return _clean_name(m.group(1))
        tokens = [w.strip(".,!?;:\"'()[]{}") for w in cleaned.split()]
        tokens = [w for w in tokens if w]
        if 1 <= len(tokens) <= 3:
            return _clean_name(tokens[-1])
    return None


def extract_introduced_name(transcript: str) -> str | None:
    """Extract the new person's name when the master introduces them ('Jarvis, this is David')."""
    if m := _INTRODUCED_NAME_RE.search(transcript.strip()):
        return _clean_name(m.group(1))
    return None


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


def _unit(emb: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(emb))
    return (emb / norm).astype(np.float32) if norm > 0 and np.isfinite(norm) else emb.astype(np.float32)


@dataclass
class VoiceProfile:
    """A speaker's voice fingerprint profile, initially unnamed and linked once they state their name."""

    id: str
    voiceprint: np.ndarray
    name: str | None = None
    clips: int = 1
    enrolled: bool = False
    greeted: bool = False
    _embeddings: list[np.ndarray] = field(default_factory=list, repr=False)

    def add_embedding(self, emb: np.ndarray) -> None:
        unit = _unit(emb)
        if not self._embeddings:
            self._embeddings = [_unit(self.voiceprint)] * max(1, self.clips)
        if len(self._embeddings) < MAX_PROFILE_CLIPS:
            self._embeddings.append(unit)
        self.voiceprint = np.mean(self._embeddings, axis=0).astype(np.float32)
        self.clips += 1


class SpeakerRegistry:
    """Tracks voice fingerprints for every heard speaker and persists named/enrolled profiles in SPEAKERS_DIR."""

    def __init__(self, folder: Path | None = SPEAKERS_DIR) -> None:
        self.folder = folder
        self.profiles: dict[str, VoiceProfile] = {}
        self._next_id = 1
        if self.folder is not None:
            self._load()

    def match(self, emb: np.ndarray | None, threshold: float) -> VoiceProfile | None:
        """Return the closest profile at or above `threshold`, or None."""
        if emb is None or not self.profiles:
            return None
        best_profile: VoiceProfile | None = None
        best_score = threshold
        for profile in self.profiles.values():
            score = cosine(emb, profile.voiceprint)
            if score >= best_score:
                best_score = score
                best_profile = profile
        return best_profile

    def observe(self, emb: np.ndarray | None, threshold: float) -> VoiceProfile | None:
        """Match a heard voice embedding to an existing profile (refining it) or create a new fingerprint profile."""
        if emb is None:
            return None
        norm = float(np.linalg.norm(emb))
        if norm == 0.0 or not np.isfinite(norm):
            return None
        matched = self.match(emb, threshold)
        if matched is not None:
            was_under_cap = len(matched._embeddings) < MAX_PROFILE_CLIPS
            matched.add_embedding(emb)
            if matched.enrolled and was_under_cap:
                self._save()
            return matched
        while f"voice_{self._next_id}" in self.profiles:
            self._next_id += 1
        pid = f"voice_{self._next_id}"
        self._next_id += 1
        unit = _unit(emb)
        profile = VoiceProfile(id=pid, voiceprint=unit, clips=1, _embeddings=[unit])
        self.profiles[pid] = profile
        return profile

    def get(self, key: str | None) -> VoiceProfile | None:
        """Look up a profile by its id ('voice_1') or linked speaker name ('David')."""
        if not key:
            return None
        if key in self.profiles:
            return self.profiles[key]
        low = key.lower()
        return next((p for p in self.profiles.values() if p.name and p.name.lower() == low), None)

    def link_name(self, profile_id: str, name: str, *, enroll: bool = True) -> VoiceProfile | None:
        """Associate a speaker identity ('David') with a voice fingerprint profile and persist if enrolled."""
        profile = self.get(profile_id)
        cleaned = _clean_name(name) or name.strip()
        if profile is None or not cleaned:
            return None
        existing = self.get(cleaned)
        if existing is not None and existing is not profile:
            for emb in profile._embeddings or [_unit(profile.voiceprint)]:
                existing.add_embedding(emb)
            self.profiles.pop(profile.id, None)
            profile = existing
        profile.name = cleaned
        if enroll:
            profile.enrolled = True
            self._save()
        return profile

    def _load(self) -> None:
        if self.folder is None:
            return
        index_path = self.folder / "speakers.json"
        if not index_path.is_file():
            return
        try:
            entries = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(f"Could not read speaker profiles from {index_path} ({exc!r}).")
            return
        for pid, meta in entries.items():
            npy_path = self.folder / f"{pid}.npy"
            if not isinstance(meta, dict) or not npy_path.is_file():
                continue
            try:
                vec = np.load(npy_path).astype(np.float32)
            except (OSError, ValueError):
                continue
            clips = int(meta.get("clips", 1))
            name = str(meta["name"]) if meta.get("name") else None
            self.profiles[pid] = VoiceProfile(
                id=pid,
                voiceprint=vec,
                name=name,
                clips=clips,
                enrolled=True,
                _embeddings=[_unit(vec)] * max(1, min(clips, MAX_PROFILE_CLIPS)),
            )

    def _save(self) -> None:
        if self.folder is None:
            return
        try:
            self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            index: dict[str, dict[str, str | int]] = {}
            for pid, profile in self.profiles.items():
                if not profile.enrolled or not profile.name:
                    continue
                npy_path = self.folder / f"{pid}.npy"
                np.save(npy_path, profile.voiceprint)
                os.chmod(npy_path, 0o600)
                index[pid] = {"name": profile.name, "clips": profile.clips}
            index_path = self.folder / "speakers.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.chmod(index_path, 0o600)
        except OSError as exc:
            logger.warning(f"Could not save speaker profiles to {self.folder} ({exc!r}).")


def save_voiceprint(embeddings: list[np.ndarray], path: Path = VOICEPRINT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Each clip scaled to length 1 first, so one loud or odd clip doesn't pull the average toward itself
    np.save(path, np.mean([e / np.linalg.norm(e) for e in embeddings], axis=0))
    return path


def load_voiceprint(path: Path = VOICEPRINT_PATH) -> np.ndarray:
    if not path.exists():
        raise FileNotFoundError(f"No owner voiceprint at {path}. Run `jarvis enroll` first.")
    return np.load(path)

