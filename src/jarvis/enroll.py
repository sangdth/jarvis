"""
Record the owner's voiceprint so Jarvis acts only on your voice.
Usage:
    uv run jarvis-enroll
"""

from __future__ import annotations

import sys

import numpy as np

from jarvis.speaker import SAMPLE_RATE, SpeakerEmbedder, cosine, save_voiceprint

SECONDS_PER_CLIP = 6
PROMPTS = [
    "Hey Jarvis, open the jarvis project and run the tests, then tell me what failed.",
    "What's on my calendar tomorrow, and did anyone reply to the invoice email?",
    "Tell Hermes to book a table for two on Friday evening, somewhere near the office.",
]


def record(seconds: int) -> bytes:
    import pyaudio

    pa = pyaudio.PyAudio()
    stream = pa.open(rate=SAMPLE_RATE, channels=1, format=pyaudio.paInt16, input=True, frames_per_buffer=1024)
    try:
        return stream.read(SAMPLE_RATE * seconds, exception_on_overflow=False)
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


def main() -> None:
    embedder = SpeakerEmbedder()
    print("\n=== Jarvis owner voice enrollment ===")
    print("Use the mic Jarvis will use. Read each line in your normal voice.\n")
    embeddings = []
    for i, prompt in enumerate(PROMPTS, 1):
        input(f"[{i}/{len(PROMPTS)}] Press Enter, then read:\n    {prompt}\n")
        print(f"Recording {SECONDS_PER_CLIP} s...")
        emb = embedder.embed(record(SECONDS_PER_CLIP))
        if emb is None:
            sys.exit("Recording was empty; check the mic and try again.")
        embeddings.append(emb)

    # Each clip against the average of the others: low numbers mean a noisy or inconsistent recording.
    for i, emb in enumerate(embeddings):
        others = np.mean([e for j, e in enumerate(embeddings) if j != i], axis=0)
        print(f"Clip {i + 1} matches the others at {cosine(emb, others):.2f}")
    print(f"\nSaved owner voiceprint to {save_voiceprint(embeddings)}")
    print("Tune SPEAKER_THRESHOLD from the [Speaker] score lines in Jarvis's log.")


if __name__ == "__main__":
    main()
