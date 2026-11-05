"""
Record the owner's voiceprint so Jarvis acts only on your voice.
Usage:
    jarvis enroll   (also the last step of `jarvis init`)
"""

from __future__ import annotations

import sys
import time

import numpy as np

from jarvis.speaker import SAMPLE_RATE, SpeakerEmbedder, cosine, save_voiceprint

SECONDS_PER_CLIP = 4
# (where to face, what to say): head turned each way, then straight on, so the voiceprint covers how the mic hears
# you at an angle too. Counting is short; the straight-on lines add everyday speech sounds.
PROMPTS = [
    ("Turn your head to the left", "One, two, three, four, five."),
    ("Turn your head to the right", "One, two, three, four, five."),
    ("Face the screen", "Hey Jarvis, open the jarvis project and run the tests."),
    ("Face the screen", "What's on my calendar tomorrow?"),
    ("Face the screen", "Tell Hermes to book a table for two on Friday."),
]


def main() -> None:
    from jarvis.mac_audio import MacVoiceAudio, MicConverter  # imported here: loads AVFoundation

    embedder = SpeakerEmbedder()
    # Record through the same voice processing Jarvis listens through, so the voiceprint sounds like what it hears.
    # One engine for all clips: a second voice-processing engine in one process records nothing.
    audio, mic, clip = MacVoiceAudio(), MicConverter(SAMPLE_RATE), []
    audio.on_mic(lambda samples, rate: clip.append(mic.convert(samples, rate)))
    audio.start()
    print("\n=== Jarvis owner voice enrollment ===")
    print("Use the mic Jarvis will use. Read each line in your normal voice.\n")
    embeddings = []
    for i, (pose, prompt) in enumerate(PROMPTS, 1):
        input(f"[{i}/{len(PROMPTS)}] {pose}. Press Enter, then say:\n    {prompt}\n")
        print(f"Recording {SECONDS_PER_CLIP} s...")
        clip.clear()
        time.sleep(SECONDS_PER_CLIP)
        emb = embedder.embed(b"".join(list(clip)))
        if emb is None:
            sys.exit("Recording was empty; check the mic and try again.")
        embeddings.append(emb)

    audio.stop()

    # Each clip against the average of the others: low numbers mean a noisy or inconsistent recording.
    for i, emb in enumerate(embeddings):
        others = np.mean([e for j, e in enumerate(embeddings) if j != i], axis=0)
        print(f"Clip {i + 1} matches the others at {cosine(emb, others):.2f}")
    print(f"\nSaved owner voiceprint to {save_voiceprint(embeddings)}")
    print("Tune SPEAKER_THRESHOLD from the [Speaker] score lines in Jarvis's log.")


if __name__ == "__main__":
    main()
