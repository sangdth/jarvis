"""Owner voice check: sherpa-onnx speaker embeddings (3D-Speaker ERes2Net, English, VoxCeleb).

Research and model choice: docs/research/speaker-verification.md.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import EllipsisType

import numpy as np
from loguru import logger

import jarvis_core.soul as soul_mod
from jarvis_voice.config import JARVIS_HOME

SAMPLE_RATE = 16000
MODEL_NAME = "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"
# Pinned to a commit on csukuangfj's Hugging Face mirror: k2-fsa/sherpa-onnx's `speaker-recongition-models`
# GitHub release is a rolling tag whose assets are overwritten in place when upstream re-exports them.
MODEL_REV = "a8055d6daef723a6a4ecf4de233708575bf4994f"
MODEL_URL = f"https://huggingface.co/csukuangfj/speaker-embedding-models/resolve/{MODEL_REV}/{MODEL_NAME}"
MODEL_SHA256 = "c4947caf150b8502fdd75bbe4089b971ab1ec42e90ee3ba547faca2cde44d9ee"
# Identical weights from upstream's earlier PyTorch 2.4.1 export, kept so existing installs don't re-download 25 MB.
_VALID_MODEL_SHA256S = {
    MODEL_SHA256,
    "c59158379255ad66e161679cca6af8d52d51e389e3224ab7d7a7baae295c2db5",
}
MODEL_PATH = JARVIS_HOME / "models" / MODEL_NAME
VOICEPRINT_PATH = JARVIS_HOME / "owner_voice.npy"
SPEAKERS_DIR = JARVIS_HOME / "speakers"
MAX_PROFILE_CLIPS = 10
FRAME = SAMPLE_RATE // 50  # 20 ms
# An unnamed voice heard in the room lives in RAM only: it is dropped after this idle time, or when a ninth voice
# needs its slot, so a day of TV never reaches the disk.
EPHEMERAL_TTL_SECS = 600.0
MAX_EPHEMERAL = 8
# Two stored profiles this close are the same voice unless the margin picks one clearly.
MATCH_MARGIN = 0.04
# A guest voiceprint needs about 4 s of clean speech to generalize.
ENOUGH_SPEECH_SECS = 4.0
# Owner enrollment runs a fuller chat, then confirms with two short answers before locking in.
OWNER_ENROLL_CLIPS = 4
OWNER_ENROLL_SECS = 10.0
OWNER_CONFIRM_CHECKS = 2
# During owner enrollment, a short reply just under the threshold triggers a retry instead of silence.
OWNER_WEAK_MARGIN = 0.12
# Log-odds the belief loop starts from: one person in ten in the room is the name just introduced.
BASE_LOG_ODDS = -2.20
INTRO_BOOST = 2.40
INTRO_FLAT_SECS = 10.0
INTRO_TAU_SECS = 30.0
INTRO_WINDOW_SECS = 90.0
# "Jarvis, meet my friend" without a name still makes the next voice's stated name much more likely.
UNNAMED_INTRO_BOOST = 0.90
PITCH_MATCH_BOOST = 0.65
PITCH_MISMATCH_BOOST = -2.50
SELF_ID_BOOST = 3.80
VOCATIVE_BOOST = 1.50
CONFIRM_BOOST = 1.20
CORRECTION_PENALTY = -4.50
# Told to the model as `[Speaker: John? (70%)]`, never spoken as a percentage.
TENTATIVE_AT = 0.50
KNOWN_AT = 0.85
MALE_MAX_HZ = 155.0
FEMALE_MIN_HZ = 180.0

_GENDER_WORDS = {
    "male": {"he", "him", "his", "brother", "husband", "boyfriend", "son", "anh", "ông", "chú", "bác"},
    "female": {"she", "her", "hers", "sister", "wife", "girlfriend", "daughter", "chị", "cô", "bà", "em gái"},
}
_AFFIRMATIVE_RE = re.compile(r"^\s*(?:yes|yeah|yep|yup|correct|right|sure|that'?s me|speaking)\b", re.IGNORECASE)
# "No, I'm Mike" / "Actually I'm Mike" / "My name is not Song, it's Sang" changes the name; "not John" drops the old one.
_CORRECTION_RE = re.compile(
    r"\b(?:(?:no|actually|sorry)\b|(?:i'?m\s+not|my\s+name\s+is\s+not|not|isn'?t)\s+[A-Za-zÀ-ỹ][\wÀ-ỹ'-]*)"
    r"[,.! ]+(?:i'?m|i am|my name is|it'?s)\s+(?!not\b)([A-Za-zÀ-ỹ][\wÀ-ỹ'-]*)",
    re.IGNORECASE,
)
_WRONG_NAME_RE = re.compile(r"\b(?:i'?m not|not|isn'?t)\s+([A-Za-zÀ-ỹ][\wÀ-ỹ'-]*)", re.IGNORECASE)
_SELF_INTRO_SHORT_RE = re.compile(
    r"^\s*(?:hi|hello|hey|good morning|good afternoon|good evening)\b[^.!?]*?\b(?:i'?m|i am)\s+"
    r"([A-Za-zÀ-ỹ][\wÀ-ỹ'-]*)",
    re.IGNORECASE,
)
_BARE_SELF_INTRO_RE = re.compile(r"^\s*(?:i'?m|i am|my name is)\s+([A-Za-zÀ-ỹ][\wÀ-ỹ'-]*)", re.IGNORECASE)

# Words that follow "I'm ..." or "this is ..." in normal speech and are never a person's name.
_NON_NAMES = {
    "a", "an", "the", "my", "your", "his", "her", "our", "their", "this", "that", "here", "there", "back", "tired",
    "good", "fine", "great", "okay", "ok", "well", "ready", "busy", "done", "free", "home", "late", "early", "going",
    "working", "talking", "listening", "looking", "thinking", "trying", "asking", "saying", "doing", "having",
    "feeling", "just", "still", "also", "very", "so", "too", "not", "no", "yes", "sure", "sorry", "happy", "glad",
    "excited", "new", "in", "on", "at", "from", "with", "for", "about", "someone", "thanks", "i", "me", "you", "it",
    "we", "they", "don't", "dont", "can't", "cant", "um", "uh", "thank", "know", "please", "hello", "hi", "hey", "what",
    "why", "how", "nothing", "again", "somebody", "anyone", "friend", "brother", "sister", "wife", "husband", "partner",
    "colleague", "coworker", "guest", "mom", "mother", "dad", "father", "son", "daughter", "jarvis", "javis", "bạn",
    "anh", "em", "chị", "mình", "tôi", "con", "người", "ai", "đây", "đó",
}

_EXPLICIT_NAME_RE = re.compile(
    r"\b(?:my\s+name\s+is|my\s+name's|you\s+can\s+call\s+me|call\s+me|tên\s+(?:mình|tôi|anh|em|chị)\s+là)\s+"
    r"(?!not\b)([A-Za-zÀ-ỹ][\wÀ-ỹ'-]*(?:\s+[A-Za-zÀ-ỹ][\wÀ-ỹ'-]*)?)",
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
    r"\b(?i:this\s+is|meet|say\s+(?:hi|hello)\s+to|introduce(?:\s+you\s+to)?|đây\s+là|làm\s+quen\s+với|giới\s+thiệu(?:\s+với\s+\w+)?)"
    r"(?:\s+(?:my|our|a|the))?"
    r"(?:\s+(?:good|best|new|close))?"
    r"(?:\s+(?:friend|brother|sister|wife|husband|partner|colleague|coworker|guest|son|daughter|bạn|anh|em|chị))?"
    r"\s*,?\s*([A-ZÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ][\wÀ-ỹ'-]*)"
)


def _clean_name(raw: str) -> str | None:
    words = [w.strip(".,!?;:\"'()[]{}") for w in raw.split()]
    words = [w for w in words if w and w.lower() not in _NON_NAMES and not w.isdigit()]
    if not words:
        return None
    name = words[0]
    return name if name[0].isupper() else name.title()


def normalize_speaker_name(raw: str) -> str | None:
    """Clean an explicitly given speaker name (keeps multi-word names like 'Minh Anh' and intentional casing)."""
    first = next((line for line in raw.splitlines() if line.strip()), "")
    cleaned = " ".join(first.replace('"', "").split()).strip(".,!?;:'()[]{}")
    if not cleaned or not any(ch.isalpha() for ch in cleaned):
        return None
    cleaned = cleaned[:60].strip()
    return cleaned if any(ch.isupper() for ch in cleaned) else cleaned.title()


def _file_sig(path: Path | None) -> tuple[int, int] | None:
    if path is None:
        return None
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


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
        # A bare reply to "What should I call you?": at most two spoken words ("David", "David Nguyen", "just David");
        # anything longer ("pretty good thanks") is small talk, so Jarvis asks again instead of guessing.
        words = [w.strip(".,!?;:\"'()[]{}") for w in cleaned.split()]
        words = [w for w in words if w and w.lower() not in _NON_NAMES and not w.isdigit()]
        if 1 <= len(words) <= 2 and len(cleaned.split()) <= 2:
            return _clean_name(words[0])
    return None


def extract_introduced_name(transcript: str) -> str | None:
    """Extract the new person's name when the master introduces them ('Jarvis, this is David')."""
    if m := _INTRODUCED_NAME_RE.search(transcript.strip()):
        return _clean_name(m.group(1))
    return None


def extract_gender_hint(transcript: str) -> str | None:
    """`male` / `female` from the pronouns and kinship words in an introduction, else None."""
    words = set(re.findall(r"[A-Za-zÀ-ỹ']+", transcript.lower()))
    male = len(words & _GENDER_WORDS["male"])
    female = len(words & _GENDER_WORDS["female"])
    if male == female:
        return None
    return "male" if male > female else "female"


def extract_intro_cue(transcript: str) -> tuple[str | None, str | None]:
    """The name and gender an introduction suggests ('This is John, he's my friend')."""
    name = extract_introduced_name(transcript)
    if name is None:
        return None, None
    return name, extract_gender_hint(transcript)


def extract_vocative_name(transcript: str, names: list[str]) -> str | None:
    """The guest the master hands the turn to ('What do you think, John?')."""
    text = transcript.strip()
    if not text.endswith("?"):
        return None
    for name in names:
        if re.search(rf"(?:^|,\s*|hey\s+){re.escape(name)}\s*\??,?\s*$", text, re.IGNORECASE):
            return name
    return None


def extract_correction(transcript: str) -> tuple[str | None, str | None]:
    """A name correction: the right name ('No, I'm Mike') and the wrong one ('I'm not John')."""
    right, wrong = _CORRECTION_RE.search(transcript), _WRONG_NAME_RE.search(transcript)
    return (
        _clean_name(right.group(1)) if right else None,
        _clean_name(wrong.group(1)) if wrong else None,
    )


def extract_owner_correction(transcript: str, current_name: str) -> str | None:
    """Safe owner-name correction while tools are hidden (enrollment): explicit self-name or negating `current_name`."""
    if m := _EXPLICIT_NAME_RE.search(transcript.strip()):
        return _clean_name(m.group(1))
    right, wrong = extract_correction(transcript)
    if right and wrong and wrong.lower() == current_name.lower():
        return right
    return None


def extract_claimed_name(transcript: str, expected: list[str], *, allow_bare: bool = False) -> str | None:
    """A name a speaker states about themselves, without turning 'It's Friday' into a person.

    Accepted: an explicit 'my name is ...' / 'call me ...', a name the room is already expecting, and a short
    greeting ('Hi, I'm John'). `allow_bare` also takes a bare reply to an introduction ("I'm David").
    """
    explicit = extract_speaker_name(transcript)
    if explicit and (_EXPLICIT_NAME_RE.search(transcript) or explicit in expected):
        return explicit
    m = _SELF_INTRO_SHORT_RE.search(transcript)
    if m and len(transcript.split()) <= 8:
        return _clean_name(m.group(1))
    if allow_bare and len(transcript.split()) <= 4:
        m = _BARE_SELF_INTRO_RE.search(transcript)
        if m:
            return _clean_name(m.group(1))
    return None


def ensure_model(path: Path = MODEL_PATH) -> Path:
    """Download the model once; a file with the wrong checksum is never loaded."""
    if path.exists() and _sha256(path) in _VALID_MODEL_SHA256S:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    logger.info(f"Downloading speaker model to {path}")
    with urllib.request.urlopen(MODEL_URL, timeout=60) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out)
    actual = _sha256(tmp)
    if actual != MODEL_SHA256:
        tmp.unlink()
        raise RuntimeError(f"Speaker model download failed its checksum ({actual} != {MODEL_SHA256}): {MODEL_URL}")
    tmp.replace(path)
    return path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class AudioStats:
    """What one sentence tells us about its speaker, computed alongside Whisper."""

    embedding: np.ndarray
    speech_secs: float
    pitch_hz: float | None


def estimate_pitch(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> float | None:
    """Median F0 of the voiced frames (autocorrelation, 80-300 Hz); None when too short or too quiet.

    Only a soft cue: a deep female and a high male overlap around 155-180 Hz, so callers must not treat it as proof.
    """
    frame = int(0.03 * sample_rate)
    hop = int(0.01 * sample_rate)
    min_lag = int(sample_rate / 300)
    max_lag = int(sample_rate / 80)
    if len(samples) < frame * 3 or max_lag >= frame:
        return None
    frames = [samples[i : i + frame] for i in range(0, len(samples) - frame + 1, hop)]
    levels = [float(np.sqrt(np.mean(f**2))) for f in frames]
    peak = max(levels, default=0.0)
    if peak < 1e-4:
        return None
    voiced: list[tuple[float, float]] = []
    for chunk, rms in zip(frames, levels):
        if rms < peak * 0.12:
            continue
        centered = chunk - chunk.mean()
        corr = np.correlate(centered, centered, mode="full")[frame - 1 :]
        if corr[0] <= 0:
            continue
        lags = corr[min_lag : max_lag + 1]
        lag = int(np.argmax(lags)) + min_lag
        if lags[lag - min_lag] / corr[0] >= 0.45:
            voiced.append((sample_rate / lag, rms))
    if len(voiced) < 3:
        return None
    freqs = np.array([f for f, _ in voiced])
    weights = np.array([w for _, w in voiced])
    order = np.argsort(freqs)
    freqs, weights = freqs[order], weights[order]
    cumulative = np.cumsum(weights)
    return float(freqs[np.searchsorted(cumulative, cumulative[-1] / 2)])


class SpeakerEmbedder:
    def __init__(self, model_path: Path | None = None) -> None:
        import sherpa_onnx

        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model_path or ensure_model()), num_threads=2)
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)

    def analyze(self, pcm16: bytes) -> AudioStats | None:
        """Embedding, voiced seconds and pitch for 16 kHz mono 16-bit PCM; None when too short to score."""
        stream = self._extractor.create_stream()
        samples = trim_silence(np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0)
        stream.accept_waveform(SAMPLE_RATE, samples)
        stream.input_finished()
        if not self._extractor.is_ready(stream):
            return None
        embedding = np.array(self._extractor.compute(stream), dtype=np.float32)
        return AudioStats(
            embedding=embedding,
            speech_secs=len(samples) / SAMPLE_RATE,
            pitch_hz=estimate_pitch(samples),
        )

    def embed(self, pcm16: bytes) -> np.ndarray | None:
        """Embedding only; kept for callers that don't need the duration or pitch."""
        stats = self.analyze(pcm16)
        return stats.embedding if stats is not None else None


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
    speech_secs: float = 0.0
    # Log-odds that this voice is each offered name; only the tracker writes it.
    beliefs: dict[str, float] = field(default_factory=dict)
    # Set when the master introduced this voice: enough trust to keep the fingerprint on disk.
    owner_vouched: bool = False
    last_seen: float = 0.0
    _embeddings: list[np.ndarray] = field(default_factory=list, repr=False)
    _weights: list[float] = field(default_factory=list, repr=False)

    def add_embedding(self, emb: np.ndarray, speech_secs: float = 0.0) -> None:
        unit = _unit(emb)
        if not self._embeddings:
            self._embeddings = [_unit(self.voiceprint)] * max(1, self.clips)
            self._weights = [1.0] * len(self._embeddings)
        if len(self._embeddings) < MAX_PROFILE_CLIPS:
            # A sentence of a few seconds says more about a voice than a one-word greeting does.
            self._embeddings.append(unit)
            self._weights.append(max(0.5, min(speech_secs, 4.0)))
            stacked = np.stack(self._embeddings)
            weighted = np.average(stacked, axis=0, weights=np.array(self._weights))
            self.voiceprint = _unit(weighted.astype(np.float32))
        self.speech_secs += speech_secs
        self.clips += 1
        self.last_seen = time.monotonic()


@dataclass(frozen=True)
class RenameResult:
    """What changed when a speaker entry was renamed (for tool response and live dashboard telemetry)."""

    profile_id: str
    source_id: str
    previous: str | None
    name: str
    enrolled: bool


_SPEAKER_LABEL_RE = re.compile(r"^speaker\s+(\d+)$", re.IGNORECASE)


class SpeakerRegistry:
    """Tracks the owner's voiceprint (claiming the first speaker when missing) and guest profiles in SPEAKERS_DIR."""

    def __init__(
        self,
        folder: Path | None = SPEAKERS_DIR,
        *,
        owner_voiceprint: np.ndarray | None = None,
        owner_name: str | None = None,
        voiceprint_path: Path | None = ...,  # type: ignore[assignment]
        soul_path: Path | None = ...,  # type: ignore[assignment]
        on_owner_enrolled: Callable[[VoiceProfile], None] | None = None,
        confirm_checks: int = OWNER_CONFIRM_CHECKS,
    ) -> None:
        self._lock = threading.RLock()
        self.folder = folder
        self.owner_voiceprint = owner_voiceprint
        self.owner_name = owner_name
        self.claiming_owner: VoiceProfile | None = None
        self.confirm_checks = max(0, confirm_checks)
        self.confirming_owner = False
        self.owner_confirmed = 0
        self.owner_last_check_weak = False
        self._owner_just_enrolled = False
        self.voiceprint_path = (VOICEPRINT_PATH if folder is not None else None) if voiceprint_path is ... else voiceprint_path
        self._soul_path: Path | None | EllipsisType = soul_path
        self._soul_state: tuple[Path, tuple[int, int] | None] | None = None
        if (resolved := self._resolved_soul_path()) is not None:
            self._soul_state = (resolved, _file_sig(resolved))
        self._speakers_sig: tuple[int, int] | None = None
        self.on_owner_enrolled = on_owner_enrolled
        self.profiles: dict[str, VoiceProfile] = {}
        self._next_id = 1
        if self.folder is not None:
            self._load()

    def _resolved_soul_path(self) -> Path | None:
        if self._soul_path is ...:
            return soul_mod.SOUL_PATH if self.folder is not None else None
        return self._soul_path

    def refresh_from_disk(self) -> None:
        """Reload owner and guest names if `SOUL.md` or `speakers.json` changed on disk."""
        with self._lock:
            soul_path = self._resolved_soul_path()
            if soul_path is not None:
                sig = _file_sig(soul_path)
                if self._soul_state is None or self._soul_state[0] != soul_path:
                    self._soul_state = (soul_path, sig)
                    if sig is not None and not self.learning_owner:
                        disk_name = soul_mod.owner_name(soul_mod.load_soul(soul_path).personality)
                        if disk_name is not None:
                            self.owner_name = disk_name
                elif sig != self._soul_state[1]:
                    self._soul_state = (soul_path, sig)
                    if sig is not None:
                        disk_name = soul_mod.owner_name(soul_mod.load_soul(soul_path).personality)
                        if disk_name is not None:
                            self.owner_name = disk_name
                            if self.claiming_owner is not None:
                                self.claiming_owner.name = disk_name
                    else:
                        self.owner_name = None
            if self.folder is not None:
                if _file_sig(self.folder / "speakers.json") != self._speakers_sig:
                    self._load()

    @property
    def learning_owner(self) -> bool:
        """True while this registry is waiting for or verifying a new owner's voiceprint."""
        with self._lock:
            return self.owner_voiceprint is None and (self.voiceprint_path is not None or self.claiming_owner is not None)

    def owner_enrollment_status(self) -> tuple[str, int, int, float, bool]:
        """Current owner-enrollment stage: (stage, step, total, speech_secs, weak_retry)."""
        with self._lock:
            if self._owner_just_enrolled:
                return ("just_enrolled", self.confirm_checks, self.confirm_checks, 0.0, False)
            if not self.learning_owner:
                return ("idle", 0, 0, 0.0, False)
            if self.claiming_owner is None:
                return ("waiting", 0, OWNER_ENROLL_CLIPS, 0.0, False)
            if self.confirming_owner:
                return (
                    "confirming",
                    self.owner_confirmed,
                    self.confirm_checks,
                    self.claiming_owner.speech_secs,
                    self.owner_last_check_weak,
                )
            return (
                "learning",
                self.claiming_owner.clips,
                OWNER_ENROLL_CLIPS,
                self.claiming_owner.speech_secs,
                self.owner_last_check_weak,
            )

    def consume_just_enrolled(self) -> bool:
        """Return True once right after owner enrollment finishes, so the closing reply fires only once."""
        with self._lock:
            if not self._owner_just_enrolled:
                return False
            self._owner_just_enrolled = False
            return True

    def active_owner_voiceprint(self) -> np.ndarray | None:
        """The enrolled owner voiceprint, or the first speaker's in-progress voiceprint while learning it."""
        with self._lock:
            if self.owner_voiceprint is not None:
                return self.owner_voiceprint
            return self.claiming_owner.voiceprint if self.claiming_owner is not None else None

    def record_owner_clip(
        self,
        emb: np.ndarray,
        speech_secs: float,
        name: str | None = None,
        *,
        score: float | None = None,
        threshold: float = 0.0,
    ) -> VoiceProfile | None:
        """Feed one addressed turn from the first speaker; returns the finalized owner profile once confirmed."""
        graduated: VoiceProfile | None = None
        with self._lock:
            if name and not self.owner_name:
                self.owner_name = _clean_name(name) or name.strip()
            if self.owner_voiceprint is not None:
                return None
            norm = float(np.linalg.norm(emb))
            if norm == 0.0 or not np.isfinite(norm):
                return None
            unit = _unit(emb)
            weight = max(0.5, min(speech_secs, 4.0))
            if self.claiming_owner is None:
                self.claiming_owner = VoiceProfile(
                    id="owner",
                    voiceprint=unit,
                    name=self.owner_name,
                    clips=1,
                    speech_secs=speech_secs,
                    last_seen=time.monotonic(),
                    _embeddings=[unit],
                    _weights=[weight],
                )
                self.owner_last_check_weak = False
            elif score is not None and score < threshold:
                # Borderline match during enrollment: ask for a clearer reply without polluting the voiceprint.
                self.owner_last_check_weak = True
                phase = "confirmation" if self.confirming_owner else "learning"
                logger.info(f"[Speaker] Owner {phase} weak (score={score:.2f} < {threshold:.2f}); asking again")
                return None
            else:
                self.owner_last_check_weak = False
                self.claiming_owner.add_embedding(emb, speech_secs)
                if self.owner_name:
                    self.claiming_owner.name = self.owner_name
                if self.confirming_owner:
                    self.owner_confirmed += 1
                    score_note = f", score={score:.2f}" if score is not None else ""
                    logger.info(
                        f"[Speaker] Owner voice check {self.owner_confirmed}/{self.confirm_checks} passed{score_note}"
                    )
                    if self.owner_confirmed >= self.confirm_checks:
                        graduated = self._finalize_owner()
                elif (
                    self.claiming_owner.clips >= OWNER_ENROLL_CLIPS
                    and self.claiming_owner.speech_secs >= OWNER_ENROLL_SECS
                ):
                    if self.confirm_checks == 0:
                        graduated = self._finalize_owner()
                    else:
                        self.confirming_owner = True
                        self.owner_confirmed = 0
                        logger.info(
                            f"[Speaker] Owner voiceprint ready (clips={self.claiming_owner.clips}, "
                            f"speech={self.claiming_owner.speech_secs:.1f}s); confirming voice "
                            f"(0/{self.confirm_checks})"
                        )
        if graduated is not None and self.on_owner_enrolled is not None:
            self.on_owner_enrolled(graduated)
        return graduated

    def _finalize_owner(self) -> VoiceProfile:
        assert self.claiming_owner is not None
        profile = self.claiming_owner
        profile.enrolled = True
        if self.owner_name:
            profile.name = self.owner_name
        self.owner_voiceprint = profile.voiceprint
        self.claiming_owner = None
        self.confirming_owner = False
        self.owner_last_check_weak = False
        self._owner_just_enrolled = True
        if self.voiceprint_path is not None:
            save_owner_voiceprint(profile.voiceprint, self.voiceprint_path)
            logger.info(
                f"[Speaker] Saved owner voiceprint to {self.voiceprint_path} "
                f"(clips={profile.clips}, speech={profile.speech_secs:.1f}s)"
            )
        return profile

    def match(self, emb: np.ndarray | None, threshold: float) -> VoiceProfile | None:
        """Return the closest profile at or above `threshold`, or None when two profiles are too close to tell."""
        with self._lock:
            if emb is None or not self.profiles:
                return None
            scored = sorted(((cosine(emb, p.voiceprint), p) for p in self.profiles.values()), key=lambda t: t[0])
            if not scored or scored[-1][0] < threshold:
                return None
            best_score, best = scored[-1]
            if len(scored) > 1 and not best.enrolled:
                runner_score = scored[-2][0]
                if best_score - runner_score < MATCH_MARGIN:
                    return None
            return best

    def observe(
        self, emb: np.ndarray | None, threshold: float, speech_secs: float = 0.0, now: float | None = None
    ) -> VoiceProfile | None:
        """Match a heard voice embedding to an existing profile (refining it) or create a new fingerprint profile."""
        with self._lock:
            self.refresh_from_disk()
            if emb is None:
                return None
            norm = float(np.linalg.norm(emb))
            if norm == 0.0 or not np.isfinite(norm):
                return None
            now = time.monotonic() if now is None else now
            self._prune(now)
            matched = self.match(emb, threshold)
            if matched is not None:
                was_under_cap = len(matched._embeddings) < MAX_PROFILE_CLIPS
                matched.add_embedding(emb, speech_secs)
                if matched.enrolled and was_under_cap:
                    self._save()
                return matched
            anonymous = [p for p in self.profiles.values() if not p.enrolled]
            while len(anonymous) >= MAX_EPHEMERAL:
                oldest = min(anonymous, key=lambda p: p.last_seen)
                self.profiles.pop(oldest.id, None)
                anonymous.remove(oldest)
            while f"voice_{self._next_id}" in self.profiles:
                self._next_id += 1
            pid = f"voice_{self._next_id}"
            self._next_id += 1
            unit = _unit(emb)
            profile = VoiceProfile(
                id=pid, voiceprint=unit, clips=1, speech_secs=speech_secs, last_seen=now, _embeddings=[unit], _weights=[1.0]
            )
            self.profiles[pid] = profile
            return profile

    def _prune(self, now: float) -> None:
        """Drop unnamed voices that have been quiet for a while: RAM-only fingerprints, never disk."""
        for pid, profile in list(self.profiles.items()):
            if not profile.enrolled and now - profile.last_seen > EPHEMERAL_TTL_SECS:
                self.profiles.pop(pid, None)

    def get(self, key: str | None) -> VoiceProfile | None:
        """Look up a profile by its id ('voice_1') or linked speaker name ('David')."""
        with self._lock:
            if not key:
                return None
            if key in self.profiles:
                return self.profiles[key]
            low = key.lower()
            return next((p for p in self.profiles.values() if p.name and p.name.lower() == low), None)

    def find_profile(self, query: str | None) -> VoiceProfile | None:
        """Look up a guest by id ('voice_1'), dashboard label ('Speaker 1'), saved name, or top tentative belief."""
        with self._lock:
            if not query or not query.strip():
                return None
            cleaned = query.strip()
            if profile := self.get(cleaned):
                return profile
            if m := _SPEAKER_LABEL_RE.match(cleaned):
                if profile := self.profiles.get(f"voice_{m.group(1)}"):
                    return profile
            low = cleaned.lower()
            for profile in self.profiles.values():
                if profile.beliefs:
                    best, _ = SpeakerBeliefTracker._probability(profile.beliefs)
                    if best and best.lower() == low:
                        return profile
            return None

    def set_owner_name(self, name: str, *, overwrite: bool = True) -> RenameResult | None:
        """Set or rename the owner in RAM and `SOUL.md`."""
        cleaned = normalize_speaker_name(name)
        if not cleaned:
            return None
        with self._lock:
            self.refresh_from_disk()
            previous = self.owner_name
            if previous and not overwrite:
                return None
            self.owner_name = cleaned
            if self.claiming_owner is not None:
                self.claiming_owner.name = cleaned
            soul_path = self._resolved_soul_path()
            if soul_path is not None:
                soul_mod.remember_owner_name(cleaned, soul_path, overwrite=overwrite)
                self._soul_state = (soul_path, _file_sig(soul_path))
            return RenameResult("owner", "owner", previous, cleaned, self.owner_voiceprint is not None)

    def rename_profile(
        self, target: str, new_name: str, *, enroll: bool = True, allow_merge: bool = True
    ) -> RenameResult | None:
        """Rename a guest profile (and persist to `speakers.json` if enrolled or `enroll=True`)."""
        cleaned = normalize_speaker_name(new_name)
        if not cleaned:
            return None
        with self._lock:
            self.refresh_from_disk()
            profile = self.find_profile(target)
            if profile is None:
                return None
            source_id = profile.id
            previous = profile.name
            if not previous and profile.beliefs:
                previous, _ = SpeakerBeliefTracker._probability(profile.beliefs)
            if not previous:
                previous = f"Speaker {speaker_number(profile)}"
            existing = self.get(cleaned)
            if existing is not None and existing is not profile:
                if not allow_merge and existing.enrolled:
                    return None
                for emb in profile._embeddings or [_unit(profile.voiceprint)]:
                    existing.add_embedding(emb, 1.0)
                existing.speech_secs += profile.speech_secs
                existing.owner_vouched = existing.owner_vouched or profile.owner_vouched
                self.profiles.pop(profile.id, None)
                if self.folder is not None and profile.enrolled:
                    (self.folder / f"{profile.id}.npy").unlink(missing_ok=True)
                profile = existing
            profile.name = cleaned
            profile.beliefs = {cleaned: 8.0}
            if enroll:
                profile.owner_vouched = True
                profile.enrolled = True
            if profile.enrolled:
                self._save()
            return RenameResult(profile.id, source_id, previous, cleaned, profile.enrolled)

    def format_for_prompt(self, *, can_rename: bool = True) -> str:
        """Live authoritative snapshot of the Speakers table for the conversation model."""
        with self._lock:
            self.refresh_from_disk()
            if not can_rename and not self.owner_name and not self.profiles:
                return ""
            owner = self.owner_name or "(unnamed)"
            guests: list[str] = []
            for p in sorted(self.profiles.values(), key=lambda x: (not x.enrolled, (x.name or x.id).lower())):
                if p.name:
                    guests.append(p.name)
                elif p.beliefs:
                    best, conf = SpeakerBeliefTracker._probability(p.beliefs)
                    label = (
                        f"{best}? (Speaker {speaker_number(p)})"
                        if best and conf >= TENTATIVE_AT
                        else f"Speaker {speaker_number(p)}"
                    )
                    guests.append(label)
                else:
                    guests.append(f"Speaker {speaker_number(p)}")
            lines = [f"- Owner: {owner}"]
            if guests:
                lines.append(f"- Guests: {', '.join(guests)}")
            action = (
                "\nIf someone says their name or a speaker's name is wrong, or asks to change/spell it, call"
                " `rename_speaker` right away so the Speakers table updates in real time."
                if can_rename
                else ""
            )
            return (
                "[Not the user speaking: Speakers table (single source of truth for speaker names; always use"
                " these over older chat or memory):\n"
                + "\n".join(lines)
                + action
                + "]"
            )

    def link_name(self, profile_id: str, name: str, *, enroll: bool = True) -> VoiceProfile | None:
        """Associate a speaker identity ('David') with a voice fingerprint profile and persist if enrolled."""
        with self._lock:
            profile = self.get(profile_id)
            cleaned = _clean_name(name) or name.strip()
            if profile is None or not cleaned:
                return None
            existing = self.get(cleaned)
            if existing is not None and existing is not profile:
                for emb in profile._embeddings or [_unit(profile.voiceprint)]:
                    existing.add_embedding(emb, 1.0)
                existing.speech_secs += profile.speech_secs
                existing.owner_vouched = existing.owner_vouched or profile.owner_vouched
                for belief_name, value in profile.beliefs.items():
                    existing.beliefs[belief_name] = max(existing.beliefs.get(belief_name, BASE_LOG_ODDS), value)
                self.profiles.pop(profile.id, None)
                if self.folder is not None and profile.enrolled:
                    (self.folder / f"{profile.id}.npy").unlink(missing_ok=True)
                profile = existing
            profile.name = cleaned
            if enroll:
                profile.enrolled = True
            if profile.enrolled:
                self._save()
            return profile

    def _load(self) -> None:
        with self._lock:
            if self.folder is None:
                return
            index_path = self.folder / "speakers.json"
            if not index_path.is_file():
                for pid in [pid for pid, p in self.profiles.items() if p.enrolled]:
                    self.profiles.pop(pid, None)
                self._speakers_sig = None
                return
            try:
                entries = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning(f"Could not read speaker profiles from {index_path} ({exc!r}).")
                return
            if not isinstance(entries, dict):
                return
            seen_ids: set[str] = set()
            for pid, meta in entries.items():
                npy_path = self.folder / f"{pid}.npy"
                if not isinstance(meta, dict) or not npy_path.is_file():
                    continue
                clips = int(meta.get("clips", 1))
                name = str(meta["name"]).strip() if meta.get("name") else None
                speech_secs = float(meta.get("speech_secs", 0.0))
                vouched = bool(meta.get("vouched", False))
                seen_ids.add(pid)
                if pid in self.profiles:
                    existing = self.profiles[pid]
                    existing.name = name
                    existing.clips = clips
                    existing.speech_secs = speech_secs
                    existing.enrolled = True
                    existing.owner_vouched = vouched
                    continue
                try:
                    vec = np.load(npy_path).astype(np.float32)
                except (OSError, ValueError):
                    continue
                self.profiles[pid] = VoiceProfile(
                    id=pid,
                    voiceprint=vec,
                    name=name,
                    clips=clips,
                    speech_secs=speech_secs,
                    enrolled=True,
                    owner_vouched=vouched,
                    last_seen=time.monotonic(),
                    _embeddings=[_unit(vec)] * max(1, min(clips, MAX_PROFILE_CLIPS)),
                    _weights=[1.0] * max(1, min(clips, MAX_PROFILE_CLIPS)),
                )
            for pid in [pid for pid, p in self.profiles.items() if p.enrolled and pid not in seen_ids]:
                self.profiles.pop(pid, None)
            self._speakers_sig = _file_sig(index_path)

    def _save(self) -> None:
        with self._lock:
            if self.folder is None:
                return
            try:
                self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
                index: dict[str, dict[str, str | int | float | bool]] = {}
                for pid, profile in self.profiles.items():
                    if not profile.enrolled or not profile.name:
                        continue
                    npy_path = self.folder / f"{pid}.npy"
                    npy_tmp = self.folder / f"{pid}.tmp.npy"
                    with os.fdopen(os.open(npy_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
                        np.save(f, profile.voiceprint)
                    os.chmod(npy_tmp, 0o600)
                    npy_tmp.replace(npy_path)
                    index[pid] = {
                        "name": profile.name,
                        "clips": profile.clips,
                        "speech_secs": round(profile.speech_secs, 2),
                        "vouched": profile.owner_vouched,
                    }
                index_path = self.folder / "speakers.json"
                index_tmp = index_path.with_suffix(".tmp")
                with os.fdopen(os.open(index_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                    f.write(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
                os.chmod(index_tmp, 0o600)
                index_tmp.replace(index_path)
                self._speakers_sig = _file_sig(index_path)
            except OSError as exc:
                logger.warning(f"Could not save speaker profiles to {self.folder} ({exc!r}).")


@dataclass
class PendingIntro:
    """A name the master just offered the room, waiting for one voice to claim it."""

    name: str | None
    gender_hint: str | None
    created_at: float
    claimed_by: set[str] = field(default_factory=set)
    pitch_scored: set[str] = field(default_factory=set)


@dataclass
class GuestBelief:
    name: str | None
    confidence: float
    graduated: bool = False


class SpeakerBeliefTracker:
    """Which voice is which name: independence-assuming log-odds updated from conversation, not a script.

    An introduction offers a name with a decaying boost (+2.4, 30 s half-life); a plausible pitch match adds a
    little, a clear mismatch takes a lot away; "I'm John" adds a lot. For one voice the score is a sigmoid; when
    two voices compete, each name is normalized across them, so only one can be John.
    """

    def __init__(self) -> None:
        self.pending: list[PendingIntro] = []
        self.vocative: tuple[str, float] | None = None
        self.last_asked: tuple[str, str] | None = None  # (profile id, name) Jarvis used tentatively

    def note_intro(self, name: str | None, gender_hint: str | None, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._expire(now)
        self.pending.append(PendingIntro(name=name, gender_hint=gender_hint, created_at=now))

    def owner_turn(self, transcript: str, known_names: list[str], now: float | None = None) -> str | None:
        """Read the master's sentence: a new introduction, or a handoff to a guest already known."""
        now = time.monotonic() if now is None else now
        name, gender = extract_intro_cue(transcript)
        if name:
            self.note_intro(name, gender, now)
        else:
            vocative = extract_vocative_name(transcript, known_names)
            if vocative:
                self.vocative = (vocative, now)
        return name

    def observe_guest(
        self,
        profile: VoiceProfile,
        transcript: str,
        *,
        pitch_hz: float | None,
        now: float | None,
        meeting: bool,
        registry: SpeakerRegistry,
    ) -> GuestBelief:
        """Update this voice's beliefs and return the best name with its probability."""
        now = time.monotonic() if now is None else now
        self._expire(now)
        if profile.enrolled and profile.name:
            return GuestBelief(profile.name, 1.0)

        expected = [p.name for p in self.pending if p.name]
        unnamed_intro = any(p.name is None for p in self.pending)
        for pending in self.pending:
            if not pending.name:
                if unnamed_intro:
                    profile.owner_vouched = True
                continue
            elapsed = now - pending.created_at
            inner = max(0.0, elapsed - INTRO_FLAT_SECS)
            boost = INTRO_BOOST * math.exp(-inner / INTRO_TAU_SECS)
            first = profile.id not in pending.claimed_by
            pending.claimed_by.add(profile.id)
            if not first:
                boost *= 0.5  # the first voice to answer the introduction is the likely one
            score = profile.beliefs.get(pending.name, BASE_LOG_ODDS) + boost
            if pending.gender_hint and pitch_hz and profile.id not in pending.pitch_scored:
                pending.pitch_scored.add(profile.id)
                if pending.gender_hint == "male":
                    score += PITCH_MATCH_BOOST if pitch_hz < MALE_MAX_HZ else (
                        PITCH_MISMATCH_BOOST if pitch_hz > FEMALE_MIN_HZ else 0.0
                    )
                else:
                    score += PITCH_MATCH_BOOST if pitch_hz > FEMALE_MIN_HZ else (
                        PITCH_MISMATCH_BOOST if pitch_hz < MALE_MAX_HZ else 0.0
                    )
            profile.beliefs[pending.name] = score
            profile.owner_vouched = True

        known_names = {p.name for p in registry.profiles.values() if p.name}
        if self.vocative and now - self.vocative[1] <= 15.0 and self.vocative[0] in (set(expected) | known_names):
            name = self.vocative[0]
            profile.beliefs[name] = profile.beliefs.get(name, BASE_LOG_ODDS) + VOCATIVE_BOOST
            profile.owner_vouched = True
            self.vocative = None

        claimed = extract_claimed_name(transcript, expected, allow_bare=bool(self.pending))
        if claimed:
            boost = SELF_ID_BOOST + (UNNAMED_INTRO_BOOST if unnamed_intro else 0.0)
            profile.beliefs[claimed] = min(profile.beliefs.get(claimed, BASE_LOG_ODDS) + boost, 8.0)

        new_name, wrong_name = extract_correction(transcript)
        if wrong_name:
            profile.beliefs[wrong_name] = profile.beliefs.get(wrong_name, BASE_LOG_ODDS) + CORRECTION_PENALTY
            for other in registry.profiles.values():
                if other is not profile and wrong_name in other.beliefs:
                    other.beliefs[wrong_name] -= 2.0
        if new_name and not claimed:
            profile.beliefs[new_name] = min(profile.beliefs.get(new_name, BASE_LOG_ODDS) + SELF_ID_BOOST, 8.0)

        if self.last_asked and self.last_asked[0] == profile.id and _AFFIRMATIVE_RE.search(transcript):
            name = self.last_asked[1]
            profile.beliefs[name] = profile.beliefs.get(name, BASE_LOG_ODDS) + CONFIRM_BOOST
        self.last_asked = None

        best_name, confidence = self._probability(profile.beliefs)
        graduated = False
        if (
            best_name
            and confidence >= KNOWN_AT
            and profile.speech_secs >= ENOUGH_SPEECH_SECS
            and not profile.enrolled
            and profile.owner_vouched
        ):
            linked = registry.link_name(profile.id, best_name, enroll=True)
            if linked is not None:
                graduated = True
                best_name = linked.name or best_name
        return GuestBelief(best_name, confidence, graduated)

    def _expire(self, now: float) -> None:
        self.pending = [p for p in self.pending if now - p.created_at <= INTRO_WINDOW_SECS]
        if self.vocative and now - self.vocative[1] > 15.0:
            self.vocative = None

    @staticmethod
    def _probability(beliefs: dict[str, float]) -> tuple[str | None, float]:
        if not beliefs:
            return None, 0.0
        weights = {name: math.exp(min(score, 20.0)) for name, score in beliefs.items()}
        total = 1.0 + sum(weights.values())
        best = max(weights, key=lambda k: weights[k])
        return best, min(weights[best] / total, 0.999)


def speaker_number(profile: VoiceProfile) -> int:
    """`voice_3` -> 3, for the dashboard and logs."""
    return int(profile.id.rsplit("_", 1)[-1]) if profile.id.rsplit("_", 1)[-1].isdigit() else 0


def save_owner_voiceprint(voiceprint: np.ndarray, path: Path = VOICEPRINT_PATH) -> Path:
    """Write a unit-normalized owner voiceprint atomically with mode 0600."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
        np.save(f, _unit(voiceprint))
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


def load_voiceprint(path: Path = VOICEPRINT_PATH) -> np.ndarray:
    if not path.exists():
        raise FileNotFoundError(f"No owner voiceprint at {path}. Run `jarvis enroll` first.")
    return np.load(path)
