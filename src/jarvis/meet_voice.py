"""
`jarvis meeting`: Jarvis's voice into Google Meet, with no virtual audio device. A Chrome extension
(`defaults/meet/`) mixes this socket's audio into the mic Meet asks for, so the call hears you and Jarvis.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from loguru import logger
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.typing import Origin

PORT = 47123  # also in defaults/meet/jarvis.js
ORIGIN = "https://meet.google.com"  # browsers set Origin and pages can't fake it, so no other site can listen in
QUEUE_CHUNKS = 200  # a stalled tab drops audio past this (a few seconds), never blocks the speaker
EXTENSION = Path(__file__).parent / "defaults" / "meet"  # Chrome loads it from here, unpacked; no copy


class MeetVoice:
    """Sends Jarvis's speech (16-bit mono PCM) to every Meet tab the extension connects from."""

    def __init__(self) -> None:
        self.rate = 0
        self._clients: set[asyncio.Queue[bytes]] = set()
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
        queue: asyncio.Queue[bytes] = asyncio.Queue(QUEUE_CHUNKS)
        self._clients.add(queue)
        logger.info(f"Meeting: Google Meet tab connected ({len(self._clients)} open); the call hears Jarvis.")
        try:
            await ws.send(json.dumps({"rate": self.rate}))
            while chunk := await queue.get():  # b"" from stop(): the handler must end for the server to close
                await ws.send(chunk)
        except ConnectionClosed:
            pass
        finally:
            self._clients.discard(queue)
            logger.info("Meeting: Google Meet tab disconnected.")

    def send(self, pcm16: bytes) -> None:
        """Called on the event loop from the output transport, as each piece of speech is queued to play."""
        if not pcm16:
            return
        for queue in self._clients:
            try:
                queue.put_nowait(pcm16)
            except asyncio.QueueFull:
                pass

    async def stop(self) -> None:
        if self._server is not None:
            for queue in self._clients:
                while not queue.empty():  # unsent speech is moot once Jarvis stops; make room for the b""
                    queue.get_nowait()
                queue.put_nowait(b"")
            self._server.close()
            await self._server.wait_closed()
            self._server = None
