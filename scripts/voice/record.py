"""Record the owner reading sentences, to fine-tune Whisper on their voice.

Usage: uv run python scripts/voice/record.py [sentences.txt]

Each line of the sentence file is read aloud: Enter starts, Enter stops, then keep, redo, skip or quit. Audio goes
through the same macOS voice processing Jarvis listens through (as `jarvis enroll` does), as 16 kHz WAVs in
JARVIS_HOME/voice_data (folder 0700, files 0600), with manifest.jsonl naming each clip's text and split. A rerun
skips what is recorded. About one sentence in seven is held out for testing, chosen by its text, so adding lines
never moves one between train and test.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np

from jarvis.config import JARVIS_HOME
from jarvis.speaker import SAMPLE_RATE

DATA_DIR = JARVIS_HOME / "voice_data"
SENTENCES = Path(__file__).with_name("sentences.txt")
TEST_EVERY = 7  # one in seven sentences is held out
MIN_SECONDS = 0.4
QUIET_PEAK = 300  # a clip whose loudest sample stays under this (of 32767) recorded nothing


def read_sentences(path: Path) -> list[str]:
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return list(dict.fromkeys(line for line in lines if line and not line.startswith("#")))  # in order, no repeats


def split_of(text: str) -> str:
    """'test' for about one sentence in TEST_EVERY, fixed by the text itself."""
    return "test" if int(hashlib.sha256(text.encode()).hexdigest(), 16) % TEST_EVERY == 0 else "train"


def load_manifest(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_wav(path: Path, pcm16: bytes) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as file:
        with wave.open(file, "wb") as clip:
            clip.setnchannels(1)
            clip.setsampwidth(2)
            clip.setframerate(SAMPLE_RATE)
            clip.writeframes(pcm16)


def main() -> None:
    from jarvis.mac_audio import MacVoiceAudio, MicConverter  # imported here: loads AVFoundation

    sentences = read_sentences(Path(sys.argv[1]) if len(sys.argv) > 1 else SENTENCES)
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest_path = DATA_DIR / "manifest.jsonl"
    done = {row["text"] for row in load_manifest(manifest_path)}
    todo = [s for s in sentences if s not in done]
    print(f"\n=== Voice set for Whisper: {len(done)} recorded, {len(todo)} to go ({DATA_DIR}) ===")
    print("Use the mic Jarvis uses, where you usually sit. Say each line as you would say it to Jarvis.")
    print("Enter starts, Enter stops. After each clip: Enter keeps it, r redoes, s skips, q quits.\n")

    audio, mic, chunks = MacVoiceAudio(), MicConverter(SAMPLE_RATE), []
    audio.on_mic(lambda samples, rate: chunks.append(mic.convert(samples, rate)))
    audio.start()
    try:
        for i, text in enumerate(todo, 1):
            while True:
                input(f"[{len(done) + i}/{len(sentences)}] Press Enter, then say:\n    {text}\n")
                chunks.clear()
                started = time.monotonic()
                input("Recording... Enter to stop.")
                pcm16 = b"".join(list(chunks))
                seconds = len(pcm16) / 2 / SAMPLE_RATE
                peak = int(np.abs(np.frombuffer(pcm16, dtype=np.int16)).max()) if pcm16 else 0
                note = " (too short)" if seconds < MIN_SECONDS else " (silent? check the mic)" if peak < QUIET_PEAK else ""
                choice = input(f"{time.monotonic() - started:.1f} s{note}. Keep [Enter], redo [r], skip [s], quit [q]: ")
                choice = choice.strip().lower()
                if choice == "r":
                    continue
                if choice == "q":
                    return
                if choice != "s":
                    name = f"{hashlib.sha256(text.encode()).hexdigest()[:12]}.wav"
                    write_wav(DATA_DIR / name, pcm16)
                    with manifest_path.open("a", encoding="utf-8") as manifest:
                        manifest.write(json.dumps({"file": name, "text": text, "split": split_of(text)}) + "\n")
                    os.chmod(manifest_path, 0o600)
                break
            print()
        print(f"All {len(sentences)} sentences recorded.")
    finally:
        audio.stop()


if __name__ == "__main__":
    main()
