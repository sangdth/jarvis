"""
`jarvis meeting`: Jarvis's voice into Google Meet, with no virtual audio device. A Chrome extension
(`defaults/meet/`) mixes this socket's audio into the mic Meet asks for, so the call hears you and Jarvis.
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger
from websockets.asyncio.server import Server, ServerConnection, broadcast, serve
from websockets.exceptions import ConnectionClosed
from websockets.typing import Origin

PORT = 47123  # also in defaults/meet/jarvis.js
ORIGIN = "https://meet.google.com"  # browsers set Origin and pages can't fake it, so no other site can listen in
EXTENSION = Path(__file__).parent / "defaults" / "meet"  # Chrome loads it from here, unpacked; no copy


class MeetVoice:
    """Sends Jarvis's speech (16-bit mono PCM) to every Meet tab the extension connects from."""

    def __init__(self) -> None:
        self.rate = 0
        self._clients: set[ServerConnection] = set()
        self._server: Server | None = None

    async def start(self) -> None:
        try:
            self._server = await serve(self._handle, "127.0.0.1", PORT, origins=[Origin(ORIGIN)])
        except OSError as exc:
            logger.warning(f"Meeting: can't speak into the call, port {PORT} is taken ({exc})")
            return
        logger.info(
            "Meeting: waiting for the Google Meet tab; keep your usual mic in Meet and wear headphones. "
            f"No tab connects? Load the Chrome extension from {EXTENSION} (chrome://extensions > Load unpacked)."
        )

    async def _handle(self, ws: ServerConnection) -> None:
        self._clients.add(ws)
        logger.info(f"Meeting: Google Meet tab connected ({len(self._clients)} open); the call hears Jarvis.")
        try:
            await ws.send(json.dumps({"rate": self.rate}))
            await ws.wait_closed()
        except ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)
            logger.info("Meeting: Google Meet tab disconnected.")

    def send(self, pcm16: bytes) -> None:
        """Called on the event loop from the output transport, as each piece of speech is queued to play."""
        if pcm16:
            broadcast(self._clients, pcm16)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
