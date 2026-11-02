"""
CLI tool to enroll your voiceprint ("Talk to me only") using Picovoice Eagle.
Usage:
    jarvis-enroll
"""

from __future__ import annotations

import struct
import sys
from loguru import logger

from jarvis.config import settings


def main() -> None:
    if not settings.picovoice_access_key:
        logger.error(
            "PICOVOICE_ACCESS_KEY is not set in your .env file.\n"
            "Get a free personal key at https://console.picovoice.ai/ and set PICOVOICE_ACCESS_KEY."
        )
        sys.exit(1)

    try:
        import pveagle
        import pyaudio
    except ImportError as exc:
        logger.error(f"Missing dependency for enrollment ({exc}). Install pveagle and pyaudio.")
        sys.exit(1)

    profiler = pveagle.create_profiler(access_key=settings.picovoice_access_key)
    pa = pyaudio.PyAudio()
    audio_stream = pa.open(
        rate=profiler.sample_rate,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=profiler.min_enroll_samples,
    )

    print("\n=== Jarvis Owner Voice Enrollment ===")
    print("Speak naturally into your microphone (e.g., explain what you're working on today).")
    print("Jarvis will use this profile to ignore all other voices.\n")

    percentage = 0.0
    try:
        while percentage < 100.0:
            pcm_bytes = audio_stream.read(
                profiler.min_enroll_samples, exception_on_overflow=False
            )
            pcm = struct.unpack(f"<{profiler.min_enroll_samples}h", pcm_bytes)
            percentage, feedback = profiler.enroll(pcm)
            print(f"\rEnrollment progress: {percentage:5.1f}% ({feedback.name})", end="", flush=True)
    finally:
        audio_stream.stop_stream()
        audio_stream.close()
        pa.terminate()

    profile = profiler.export()
    profiler.delete()

    out_path = settings.resolved_voiceprint_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(profile.to_bytes())
    print(f"\n\nSaved owner voiceprint to: {out_path}")


if __name__ == "__main__":
    main()
