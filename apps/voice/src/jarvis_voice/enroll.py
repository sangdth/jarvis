"""
Enroll (or re-enroll) the owner's voiceprint through a short natural conversation with Jarvis.

On a fresh install Jarvis already claims the first person who talks to it automatically; `jarvis enroll` resets
`owner_voice.npy` so the next person to chat with Jarvis becomes its owner.
"""

from __future__ import annotations

import asyncio
import os
import sys

from jarvis_core.home import JARVIS_HOME, set_env
from jarvis_voice.config import settings
from jarvis_voice.speaker import VOICEPRINT_PATH

ENV_PATH = JARVIS_HOME / ".env"


def main() -> None:
    if not settings.openrouter_api_key:
        sys.exit(f"Set OPENROUTER_API_KEY in {ENV_PATH} first (`jarvis init`) so Jarvis can chat with you.")

    from jarvis_voice.main import run_pipecat_harness

    previous = VOICEPRINT_PATH.read_bytes() if VOICEPRINT_PATH.is_file() else None
    VOICEPRINT_PATH.unlink(missing_ok=True)
    print("\n=== Jarvis owner voice enrollment ===")
    print("Chat naturally with Jarvis for a few turns, then answer a couple of quick questions to confirm your voice.\n")
    profile = None
    try:
        profile = asyncio.run(run_pipecat_harness(enroll_owner=True))
    except KeyboardInterrupt:
        print()
    finally:
        if not VOICEPRINT_PATH.is_file() and previous is not None:
            tmp = VOICEPRINT_PATH.with_name(f"{VOICEPRINT_PATH.name}.tmp")
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
                f.write(previous)
            os.chmod(tmp, 0o600)
            tmp.replace(VOICEPRINT_PATH)
            print(f"Cancelled; kept existing voiceprint at {VOICEPRINT_PATH}.")

    if not VOICEPRINT_PATH.is_file():
        sys.exit("No speech recorded; owner voiceprint was not saved.")

    who = f" ({profile.name})" if profile and profile.name else ""
    detail = f" ({profile.clips} turn(s), {profile.speech_secs:.1f}s)" if profile else ""
    print(f"\nSaved owner voiceprint{who} to {VOICEPRINT_PATH}{detail}.")
    if not settings.master_check:
        set_env("MASTER_CHECK", "true", ENV_PATH)
        print(f"Turned on MASTER_CHECK in {ENV_PATH}: Jarvis now acts only on your voice.")


if __name__ == "__main__":
    main()
