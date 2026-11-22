"""
`jarvis remote`: Jarvis runs here, the mic and speaker are on another Mac.

The heavy half (Whisper, the speech check, the agents, the models, memory) stays on this machine. The other Mac
runs one small script (`defaults/remote/client.py`) that captures its mic through macOS voice processing and plays
what comes back, so barge-in and "shut up" still work: the echo canceller has to sit where the speaker is.

Wire: one WebSocket per listening Jarvis, 16-bit mono PCM at `SAMPLE_RATE` both ways. Audio frames are binary;
control is JSON (`hello`, `flush`). The token is a shared secret kept in `JARVIS_HOME/remote_token` (0600) and
required on every connection: with `MASTER_CHECK` off any voice may talk to Jarvis, and the port is on the LAN.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
from email.utils import formatdate
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from loguru import logger
from pipecat.frames.frames import InputAudioRawFrame, InterruptionFrame, OutputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

from jarvis_voice.config import JARVIS_HOME, settings
from jarvis_voice.voice_engine import PLAY_AHEAD_SECS

SAMPLE_RATE = 16000  # 16-bit mono both ways, the rate the pipeline runs at; the client resamples to it
PORT = 47210  # also in `jarvis remote`'s logs and the printed client command
TOKEN_PATH = JARVIS_HOME / "remote_token"
CLIENT_SCRIPT = Path(__file__).parent / "defaults" / "remote" / "client.py"
# Mic audio waiting for the pipeline, in 20 ms blocks: past a second behind, the oldest goes rather than lag.
MIC_QUEUE_BLOCKS = 50
# A write that takes this long means the other Mac stopped reading: drop that client, never mark the transport broken
# (Pipecat gives up on a transport whose write hangs past `audio_out_write_timeout_secs`, 10 s, for good).
SEND_TIMEOUT_SECS = 2.0
# A client that connects and sends nothing for this long has a silent mic (no Microphone permission, most likely).
SILENT_CLIENT_SECS = 3.0
# Where the client script reads its server from; the served copy has this replaced with this server's own URL.
CLIENT_URL_PLACEHOLDER = 'DEFAULT_URL = ""'


def relay_token() -> str:
    """The shared secret for relay clients: `REMOTE_TOKEN` if set, else one kept in `JARVIS_HOME/remote_token`."""
    if settings.remote_token:
        return settings.remote_token
    if not TOKEN_PATH.exists():
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(secrets.token_urlsafe(24))
        os.chmod(TOKEN_PATH, 0o600)
    return TOKEN_PATH.read_text().strip()


def advertised_hosts() -> list[str]:
    """Names another Mac can reach this one by: its Bonjour name and its LAN address, best effort."""
    hosts = []
    name = socket.gethostname().split(".")[0]
    if name:
        hosts.append(f"{name}.local")
    try:
        hosts.append(socket.gethostbyname(name))
    except OSError:
        pass
    return list(dict.fromkeys(hosts))


class RelayHub:
    """One relay client at a time, and the frames between it and the pipeline."""

    def __init__(self, token: str = "", port: int = PORT) -> None:
        self.token, self.port = token, port  # only to print the command to reconnect
        self.client: ServerConnection | None = None
        self.connected = asyncio.Event()
        self.mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MIC_QUEUE_BLOCKS)
        self._send_lock = asyncio.Lock()
        self.dropped_mic = 0
        self.received = 0  # bytes of mic audio from the current client, to notice a silent one

    def attach(self, ws: ServerConnection) -> bool:
        """Take the client, unless another Mac is already the mic; False means refuse it."""
        if self.client is not None:
            return False
        self.client = ws
        self._drain_mic()  # what the previous client left is no longer wanted
        self.connected.set()
        logger.info("Relay client connected: hearing its mic and speaking into its speakers.")
        return True

    def detach(self, ws: ServerConnection) -> None:
        if self.client is not ws:
            return  # a refused or replaced client, not the one in use
        self.client = None
        self._drain_mic()
        self.connected.clear()
        logger.info(
            "Relay client disconnected. "
            f"To listen from another Mac again: {remote_command(advertised_hosts(), self.token, self.port)}"
        )

    def _drain_mic(self) -> None:
        while not self.mic_queue.empty():
            self.mic_queue.get_nowait()

    def push_mic(self, pcm: bytes) -> None:
        """Audio from the client, on the server's event loop: drop the oldest rather than fall behind."""
        self.received += len(pcm)
        if self.mic_queue.full():
            self.mic_queue.get_nowait()
            self.dropped_mic += 1
        self.mic_queue.put_nowait(pcm)

    async def send_audio(self, pcm: bytes) -> float:
        """Send speech to the client to play; returns its length in seconds to pace the pipeline."""
        client = self.client
        if client is not None and pcm:
            try:
                async with self._send_lock:  # one sender at a time: a flush must not land inside a frame
                    await asyncio.wait_for(client.send(pcm), SEND_TIMEOUT_SECS)
            except ConnectionClosed:
                pass  # detach() logs it; the pipeline keeps running and drops frames until a client returns
            except TimeoutError:
                logger.warning("Relay: the client stopped reading audio; dropping it until it reconnects.")
                self.detach(client)
        return len(pcm) / 2 / SAMPLE_RATE

    async def flush(self) -> None:
        """An interruption: the client drops what it has queued but not yet played."""
        client = self.client
        if client is None:
            return
        try:
            async with self._send_lock:
                await asyncio.wait_for(client.send(json.dumps({"type": "flush"})), SEND_TIMEOUT_SECS)
        except ConnectionClosed:
            pass
        except TimeoutError:
            self.detach(client)

    async def wait_for_client(self) -> None:
        await self.connected.wait()


class RemoteVoiceInputTransport(BaseInputTransport):
    """The client's mic, as the pipeline's input."""

    def __init__(self, hub: RelayHub, params: TransportParams) -> None:
        super().__init__(params)
        self._hub = hub
        self._pump_task: asyncio.Task[None] | None = None

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        # Its own queue opens in set_transport_ready, so the pumping starts after it, never before.
        await self.set_transport_ready(frame)
        self._pump_task = self.create_task(self._pump())

    async def cleanup(self) -> None:
        await super().cleanup()
        if self._pump_task is not None:
            await self.cancel_task(self._pump_task)
            self._pump_task = None

    async def _pump(self) -> None:
        while True:
            pcm = await self._hub.mic_queue.get()
            await self.push_audio_frame(InputAudioRawFrame(audio=pcm, sample_rate=self.sample_rate, num_channels=1))


class RemoteVoiceOutputTransport(BaseOutputTransport):
    """The pipeline's speech, sent to the client to play."""

    def __init__(self, hub: RelayHub, params: TransportParams) -> None:
        super().__init__(params)
        self._hub = hub
        self._until = 0.0  # loop time when everything sent so far has been played

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        # Pace like a blocking speaker write, so an interruption drops the audio still in the pipeline instead of
        # everything already queued playing out on the other Mac.
        now = asyncio.get_running_loop().time()
        self._until = max(self._until, now) + await self._hub.send_audio(frame.audio)
        await asyncio.sleep(max(0.0, self._until - now - PLAY_AHEAD_SECS))
        return True

    async def process_frame(self, frame: Any, direction: Any) -> None:
        if isinstance(frame, InterruptionFrame):
            await self._interrupt()
        await super().process_frame(frame, direction)

    async def _interrupt(self) -> None:
        """The user talked over Jarvis: what the other Mac still has queued is no longer wanted."""
        self._until = 0.0
        await self._hub.flush()


class RemoteVoiceTransport(BaseTransport):
    """The pipeline's audio, on another Mac: this side keeps every model and every agent."""

    def __init__(self, params: TransportParams, *, port: int = PORT, token: str | None = None) -> None:
        super().__init__()
        self._params = params
        self.token = relay_token() if token is None else token
        self.port = port
        self.hub = RelayHub(self.token, port)
        self._server: Server | None = None
        self._input: RemoteVoiceInputTransport | None = None
        self._output: RemoteVoiceOutputTransport | None = None

    async def start_server(self) -> None:
        """Listen for the client; it may connect before, during or after the pipeline's own startup."""
        self._server = await serve(
            self._handle, "0.0.0.0", self.port, process_request=self._process_request, max_size=1 << 20
        )
        self.port = self._server.sockets[0].getsockname()[1]
        self.hub.port = self.port  # the printed command must name the port actually used
        command = remote_command(advertised_hosts(), self.token, self.port)
        logger.info(f"Relay listening on port {self.port}: the mic and speaker of another Mac reach this Jarvis only.")
        logger.info(f"On that Mac (needs only uv, https://docs.astral.sh/uv/):\n  {command}")

    async def stop_server(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def wait_for_client(self) -> None:
        await self.hub.wait_for_client()

    def input(self) -> FrameProcessor:
        if not self._input:
            self._input = RemoteVoiceInputTransport(self.hub, self._params)
        return self._input

    def output(self) -> FrameProcessor:
        if not self._output:
            self._output = RemoteVoiceOutputTransport(self.hub, self._params)
        return self._output

    def _process_request(self, connection: ServerConnection, request: Any) -> Response | None:
        """Serve the client script and refuse a connection without this Mac's token; both are behind the token."""
        path, _, query = request.path.partition("?")
        if not secrets.compare_digest(parse_qs(query).get("token", [""])[0], self.token):
            logger.warning(f"Relay: refused a connection from {connection.remote_address[0]} (bad token).")
            return connection.respond(401, "Jarvis relay: the token does not match this Mac's.\n")
        if path == "/client.py":
            return self._script_response()
        if path == "/ws":
            return None  # the WebSocket the client speaks on
        return connection.respond(404, "Jarvis relay: /client.py and /ws, nothing else.\n")

    def _script_response(self) -> Response:
        """The client script as a plain HTTP file (the laptop curls it), not a WebSocket."""
        body = self.client_script().encode()
        headers = Headers(
            [
                ("Date", formatdate(usegmt=True)),
                ("Connection", "close"),
                ("Content-Length", str(len(body))),
                ("Content-Type", "text/x-python; charset=utf-8"),
            ]
        )
        return Response(200, "OK", headers, body)

    def client_script(self) -> str:
        """The thin client, with this Mac's address and token filled in so the laptop needs no arguments."""
        url = f"ws://{self.public_host()}:{self.port}/ws?token={self.token}"
        return CLIENT_SCRIPT.read_text().replace(CLIENT_URL_PLACEHOLDER, f'DEFAULT_URL = "{url}"')

    def public_host(self) -> str:
        hosts = advertised_hosts()
        return hosts[0] if hosts else "127.0.0.1"

    async def _handle(self, ws: ServerConnection) -> None:
        if not self.hub.attach(ws):
            await ws.close(code=1013, reason="another Mac is already this Jarvis's mic")
            return
        self.hub.received = 0
        silence_check = asyncio.create_task(self._warn_if_silent(ws))
        try:
            await ws.send(json.dumps({"type": "hello", "rate": SAMPLE_RATE}))
            async for message in ws:
                if isinstance(message, bytes):
                    self.hub.push_mic(message)
        except ConnectionClosed:
            pass
        finally:
            silence_check.cancel()
            self.hub.detach(ws)

    async def _warn_if_silent(self, ws: ServerConnection) -> None:
        # Like the local mic's SILENT_MIC_WARNING: a connected client with no Microphone permission sends zeros,
        # or nothing at all, so this is a timer on the first block, not a check of the sound.
        await asyncio.sleep(SILENT_CLIENT_SECS)
        if self.hub.client is ws and self.hub.received == 0:
            logger.error(
                "The relay client is connected but its mic sent nothing: allow Microphone for the terminal on that "
                "Mac (System Settings > Privacy & Security > Microphone), then restart it."
            )


def remote_command(hosts: list[str], token: str, port: int = PORT) -> str:
    """What to paste on the other Mac: fetch the client from here and run it. Only uv is needed there."""
    host = hosts[0] if hosts else "127.0.0.1"
    return (
        f"curl -fsSL 'http://{host}:{port}/client.py?token={token}' -o /tmp/jarvis-remote.py"
        f" && uv run /tmp/jarvis-remote.py"
    )


def make_transport(port: int = PORT) -> RemoteVoiceTransport:
    """The transport `jarvis remote` puts in place of the local mic and speaker."""
    params = TransportParams(audio_in_enabled=True, audio_out_enabled=True, audio_in_sample_rate=SAMPLE_RATE,
                             audio_out_sample_rate=SAMPLE_RATE)
    return RemoteVoiceTransport(params, port=port)
