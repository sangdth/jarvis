"""
`jarvis` runs the whole thing: the brain and the voice harness in one process, on this checkout.

There is no build or install step between the code and what runs. The repository is the runtime, so an edit plus
`jarvis restart` is live. One HTTP/SSE/WebSocket server on `BRAIN_PORT` is the only network surface: the brain's
API, the cockpit's telemetry, the thin client's mic and speaker, and the small probe/operator routes.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from loguru import logger

from jarvis_core.home import brain_home, brain_token, shared_dir


def prepare_environment() -> None:
    """
    Settle the paths and the one token before any app builds its settings: the voice harness and the daemon
    then read the same `.env`, the same state folder and the same bearer token.

    A pre-merge `~/.config/superbrain` is adopted into `JARVIS_HOME/brain` on first run, so an older board keeps
    working after the move to a checkout-owned state home.
    """
    from jarvis_core.home import migrate_legacy_brain_home

    migrate_legacy_brain_home()
    os.environ.setdefault("BRAIN_HOME", str(brain_home()))
    os.environ.setdefault("BRAIN_API_KEY", brain_token())
    shared_dir(brain_home())


def brain_url() -> str:
    """Where the server listens, for a client: loopback when bound to a wildcard address."""
    from brain.config import settings

    return settings.endpoint_url


async def listening(url: str) -> bool | None:
    """True when a Jarvis answers there, False when nothing does, None when one refuses our token."""
    token = os.environ.get("BRAIN_API_KEY", "")
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            resp = await client.get(f"{url}/v1/status", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError:
        return False
    return True if resp.status_code == 200 else None


async def start_all(
    *,
    connect: str | None = None,
    meeting: bool = False,
    remote: bool = False,
) -> None:
    """
    Run the brain and the voice together until the pipeline ends or a restart is requested.

    `remote` puts the mic and speaker on another machine and serves the thin client from this same server.
    `RELAY_EARS=true` keeps this Mac's mic and speaker too, and the ear follows whichever one hears you.
    """
    from brain.cli import running
    from jarvis_core.devices import DeviceStore
    from jarvis_voice.config import settings

    restart = asyncio.Event()
    voice_state: dict[str, Any] = {"state": "starting", "meeting": meeting, "remote": remote, "client": None}
    devices = DeviceStore.load()

    relay: Any = None
    if not meeting and (remote or settings.relay_ears):
        from jarvis_voice.remote_audio import make_transport

        relay = make_transport(devices=devices, local=not remote)

    def voice_snapshot() -> dict[str, Any]:
        snapshot = dict(voice_state)
        if relay is not None:
            snapshot["client"] = relay.hub.client is not None
            snapshot["port"] = relay.port
        return snapshot

    extra_routes = relay.routes() if relay is not None else None
    async with running(
        on_restart=restart.set,
        extra_routes=extra_routes,
        voice_snapshot=voice_snapshot,
        devices=devices,
    ) as server:
        if relay is not None:
            relay.port = server.cfg.brain_port
            relay.hub._hint = _relay_hint(relay)
            relay.announce()
        logger.info(f"Jarvis {server.status_payload()['version']} on {server.cfg.endpoint_url}")

        from jarvis_voice.main import run_pipecat_harness

        voice = asyncio.create_task(
            run_pipecat_harness(connect=connect, meeting=meeting, remote=remote, relay=relay, status=voice_state),
            name="jarvis-voice",
        )
        stop = asyncio.create_task(restart.wait(), name="jarvis-restart")
        try:
            done, _ = await asyncio.wait({voice, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not voice.done():
                voice.cancel()
            stop.cancel()
            for task in (voice, stop):
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            voice_state["state"] = "stopped"
        if restart.is_set():
            logger.info("Restart requested: stopping cleanly so the supervisor starts the new code.")
        if voice.done() and not voice.cancelled() and voice.exception() is not None:
            raise voice.exception()  # type: ignore[misc]


def _relay_hint(relay: Any) -> str:
    from jarvis_voice.remote_audio import advertised_hosts, remote_command

    return remote_command(advertised_hosts(), relay.port)
