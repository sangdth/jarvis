"""
Record the owner's voiceprint so Jarvis acts only on your voice.
Usage:
    jarvis enroll   (also the last step of `jarvis init`)
"""

from __future__ import annotations

import sys
import time

import numpy as np

from jarvis.config import settings
from jarvis.prompts import LANGUAGES
from jarvis.speaker import SAMPLE_RATE, SpeakerEmbedder, cosine, save_voiceprint

# Room for a slow start; the silence around the sentence is trimmed before it is scored.
SECONDS_PER_CLIP = 5
# The suggested SPEAKER_THRESHOLD sits this far below your lowest check score.
THRESHOLD_MARGIN = 0.05


def suggest_threshold(scores: list[float]) -> float:
    """A threshold just under the lowest score of your own unseen sentences."""
    return round(min(scores) - THRESHOLD_MARGIN, 2)


def main() -> None:
    from jarvis.mac_audio import MacVoiceAudio, MicConverter  # imported here: loads AVFoundation

    text = LANGUAGES[settings.language]
    embedder = SpeakerEmbedder()
    # Record through the same voice processing Jarvis listens through, so the voiceprint sounds like what it hears.
    # One engine for all clips: a second voice-processing engine in one process records nothing.
    audio, mic, clip = MacVoiceAudio(), MicConverter(SAMPLE_RATE), []
    audio.on_mic(lambda samples, rate: clip.append(mic.convert(samples, rate)))
    audio.start()

    def record(label: str, line: str) -> bytes:
        input(f"{label} Press Enter, then say:\n    {line}\n")
        print(f"Recording {SECONDS_PER_CLIP} s...")
        clip.clear()
        time.sleep(SECONDS_PER_CLIP)
        return b"".join(list(clip))

    print("\n=== Jarvis owner voice enrollment ===")
    print("Use the mic Jarvis will use, where you usually sit. Read each line in your normal voice.\n")
    embeddings = []
    for i, line in enumerate(text["enroll"], 1):
        emb = embedder.embed(record(f"[{i}/{len(text['enroll'])}]", line))
        if emb is None:
            sys.exit("Recording was empty; check the mic and try again.")
        embeddings.append(emb)
    for i, emb in enumerate(embeddings):
        # Each clip against the average of the others: a low number means a noisy or inconsistent recording.
        others = np.mean([e for j, e in enumerate(embeddings) if j != i], axis=0)
        print(f"Clip {i + 1} matches the others at {cosine(emb, others):.2f}")
    path = save_voiceprint(embeddings)
    voiceprint = np.load(path)
    print(f"\nSaved owner voiceprint to {path}")

    print("\nNow three new lines, to measure how Jarvis will score you.\n")
    scores = []
    for i, line in enumerate(text["enroll_check"], 1):
        score = cosine(embedder.embed(record(f"[check {i}/{len(text['enroll_check'])}]", line)), voiceprint)
        scores.append(score)
        print(f"Score {score:.2f}")
    audio.stop()

    print(f"\nYour scores: {', '.join(f'{s:.2f}' for s in scores)} (now SPEAKER_THRESHOLD={settings.speaker_threshold}).")
    print(f"Suggested: set SPEAKER_THRESHOLD={suggest_threshold(scores):.2f} in .env, then restart Jarvis.")


if __name__ == "__main__":
    main()
