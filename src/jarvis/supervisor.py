"""
`jarvis` starts the brain with the voice. The brain (`apps/brain`) is a daemon of its own, so
`jarvis` attaches to the one already listening (the 24/7 LaunchAgent, or another window) instead of
starting a second: it has one board, one SQLite file and one reconciler.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from loguru import logger

from jarvis_core.home import brain_home, brain_token, shared_dir


def prepare_environment() -> None:
    """
    Settle the paths and the one brain token before any app builds its settings: the voice harness and
    the daemon then read the same `.env`, the same state folder and the same bearer token.
    """
    os.environ.setdefault("BRAIN_HOME", str(brain_home()))
    os.environ.setdefault("BRAIN_API_KEY", brain_token())
    shared_dir(brain_home())


def brain_url() -> str:
    """Where the daemon listens: `BRAIN_URL` from the shell or `.env`, default `http://127.0.0.1:8643`."""
    from jarvis_voice.config import settings

    return settings.brain_url.rstrip("/")


async def listening(url: str) -> bool | None:
    """True when a Brain answers there, False when nothing does, None when one refuses our token."""
    token = os.environ.get("BRAIN_API_KEY", "")
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            resp = await client.get(f"{url}/v1/telemetry/snapshot", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError:
        return False
    return True if resp.status_code == 200 else None


@asynccontextmanager
async def brain_running() -> AsyncIterator[bool]:
    """Yield True when this process started the brain, False when it attached to one already running."""
    url = brain_url()
    state = await listening(url)
    if state is None:
        raise SystemExit(
            f"Brain at {url} is running but refused Jarvis's token. Set BRAIN_API_KEY in "
            f"{os.environ['BRAIN_HOME']}/.env (or JARVIS_HOME/.env) to the same value, or stop that daemon."
        )
    if state:
        logger.info(f"The brain is already listening at {url}; using it (it keeps running after Jarvis stops).")
        yield False
        return
    from brain.cli import running  # imported here: reads the settings the lines above just settled

    async with running():
        logger.info(f"Brain started with Jarvis at {url}; it stops when Jarvis does.")
        yield True
