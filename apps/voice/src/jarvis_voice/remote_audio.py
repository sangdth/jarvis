"""
The thin client's mic and speaker, on the same HTTP server as everything else.

Jarvis runs here; the heavy half (Whisper, the speech check, the agents, the models, memory) never leaves this
Mac. Another Mac (or this one) runs one small script (`defaults/remote/client.py`) that captures its mic through
macOS voice processing and plays what comes back, so barge-in and "shut up" still work: the echo canceller has to
sit where the speaker is.

Wire: `GET /client.py` serves that script; `GET /ws?token=...` is one WebSocket per listening Jarvis with 16-bit
mono PCM at `SAMPLE_RATE` both ways. Audio frames are binary; control is JSON (`hello`, `flush`). The routes are
registered on the one Jarvis HTTP app (`BrainServer`), so a thin client needs one host, one port and one token.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
from pathlib import Path
from typing import Any, Protocol

from aiohttp import WSMsgType, web
from loguru import logger
from pipecat.frames.frames import InputAudioRawFrame, InterruptionFrame, OutputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

from jarvis_core.home import brain_token
from jarvis_voice.config import settings
from jarvis_voice.voice_engine import PLAY_AHEAD_SECS

SAMPLE_RATE = 16000  # 16-bit mono both ways, the rate the pipeline runs at; the client resamples to it
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


class RelaySocket(Protocol):
    """What `RelayHub` needs from a connected client, so a test can stand in for one."""

    async def send_bytes(self, data: bytes) -> None: ...

    async def send_text(self, data: str) -> None: ...


def relay_token() -> str:
    """The token for relay clients: the one shared gateway token (`remote_token` / `REMOTE_TOKEN` can override)."""
    if settings.remote_token:
        return settings.remote_token
    return settings.brain_api_key or brain_token()


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


def remote_command(hosts: list[str], token: str, port: int) -> str:
    """What to paste on the other Mac: fetch the client from here and run it. Only uv is needed there."""
    host = hosts[0] if hosts else "127.0.0.1"
    return (
        f"curl -fsSL 'http://{host}:{port}/client.py?token={token}' -o /tmp/jarvis-remote.py"
        f" && uv run /tmp/jarvis-remote.py"
    )


class _HttpRelaySocket:
    """Adapts one aiohttp WebSocket to what `RelayHub` and `send_audio` speak."""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self.remote = "?"

    async def send_bytes(self, data: bytes) -> None:
        await self.ws.send_bytes(data)

    async def send_text(self, data: str) -> None:
        await self.ws.send_str(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self.ws.close(code=code, message=reason.encode())


class RelayHub:
    """One relay client at a time, and the frames between it and the pipeline."""

    def __init__(self) -> None:
        self.client: RelaySocket | None = None
        self.connected = asyncio.Event()
        self.mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MIC_QUEUE_BLOCKS)
        self._send_lock = asyncio.Lock()
        self.dropped_mic = 0
        self.received = 0  # bytes of mic audio from the current client, to notice a silent one

    def attach(self, ws: RelaySocket) -> bool:
        """Take the client, unless another Mac is already the mic; False means refuse it."""
        if self.client is not None:
            return False
        self.client = ws
        self._drain_mic()  # what the previous client left is no longer wanted
        self.connected.set()
        logger.info("Relay client connected: hearing its mic and speaking into its speakers.")
        return True

    def detach(self, ws: RelaySocket) -> None:
        if self.client is not ws:
            return  # a refused or replaced client, not the one in use
        self.client = None
        self._drain_mic()
        self.connected.clear()
        logger.info(
            "Relay client disconnected. "
            f"To listen from another Mac again: {self.reconnect_hint()}"
        )

    def reconnect_hint(self) -> str:
        """The line to paste, filled in at print time since the port is only known once it listens."""
        return self._hint

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
                    await asyncio.wait_for(client.send_bytes(pcm), SEND_TIMEOUT_SECS)
            except (ConnectionError, RuntimeError):
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
                await asyncio.wait_for(client.send_text(json.dumps({"type": "flush"})), SEND_TIMEOUT_SECS)
        except (ConnectionError, RuntimeError):
            pass
        except TimeoutError:
            self.detach(client)

    async def wait_for_client(self) -> None:
        await self.connected.wait()

    _hint = ""


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
    """
    The pipeline's audio, on another Mac: this side keeps every model and every agent.

    The two routes (`/ws` and `/client.py`) are registered on the one Jarvis HTTP app; `web_app()` builds a
    standalone app for the voice-only case and the tests.
    """

    def __init__(
        self,
        params: TransportParams,
        *,
        token: str | None = None,
        hub: RelayHub | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        super().__init__()
        self._params = params
        self.token = relay_token() if token is None else token
        self.host = host or settings.brain_host
        self.port = settings.brain_port if port is None else port
        self.hub = hub or RelayHub()
        self.hub._hint = remote_command(advertised_hosts(), self.token, self.port)
        self._runner: web.AppRunner | None = None
        self._input: RemoteVoiceInputTransport | None = None
        self._output: RemoteVoiceOutputTransport | None = None

    def routes(self) -> list[web.RouteDef]:
        """The relay's two routes, to register on the shared Jarvis app."""
        return [web.get("/client.py", self.handle_client_script), web.get("/ws", self.handle_ws)]

    def web_app(self) -> web.Application:
        app = web.Application()
        app.add_routes(self.routes())
        return app

    async def start_server(self) -> None:
        """Listen on its own (the voice without the brain, and the tests). Attaching to the shared app skips this."""
        self._runner = web.AppRunner(self.web_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, host=self.host, port=self.port)
        await site.start()
        sockets = getattr(site._server, "sockets", None)
        if sockets:
            self.port = sockets[0].getsockname()[1]
        self.hub._hint = remote_command(advertised_hosts(), self.token, self.port)
        logger.info(f"Relay listening on {self.host}:{self.port}: the mic and speaker of another Mac reach this Jarvis.")
        logger.info(f"On that Mac (needs only uv, https://docs.astral.sh/uv/):\n  {self.hub.reconnect_hint()}")

    async def stop_server(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def announce(self) -> None:
        """Log the command to paste, once the shared app knows the real port."""
        logger.info(
            f"Relay on {self.public_host()}:{self.port}: the mic and speaker of another Mac reach this Jarvis."
        )
        logger.info(f"On that Mac (needs only uv, https://docs.astral.sh/uv/):\n  {self.hub.reconnect_hint()}")

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

    @staticmethod
    def _token_from(request: web.Request) -> str:
        """The token, from `Authorization: Bearer` or `?token=`, so a browser and an iOS Shortcut both work."""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth.removeprefix("Bearer ").strip()
        return (request.query.get("token") or request.headers.get("X-Jarvis-Token") or "").strip()

    def _authorized(self, request: web.Request) -> bool:
        return secrets.compare_digest(self._token_from(request), self.token)

    async def handle_client_script(self, request: web.Request) -> web.Response:
        """The thin client, as a plain HTTP file (the laptop curls it), never as a WebSocket."""
        if not self._authorized(request):
            logger.warning(f"Relay: refused {request.remote or '?'} for the client script (bad token).")
            return web.Response(status=401, text="Jarvis relay: the token does not match this Mac's.\n")
        body = self.client_script(request.url.host)
        return web.Response(
            text=body,
            content_type="text/x-python",
            charset="utf-8",
            headers={"Cache-Control": "no-store", "Content-Disposition": 'inline; filename="jarvis-remote.py"'},
        )

    async def handle_ws(self, request: web.Request) -> web.StreamResponse:
        """One WebSocket per listening Jarvis: binary audio down, binary audio up, JSON control both ways."""
        if not self._authorized(request):
            logger.warning(f"Relay: refused a connection from {request.remote} (bad token).")
            return web.Response(status=401, text="Jarvis relay: the token does not match this Mac's.\n")
        ws = web.WebSocketResponse(max_msg_size=1 << 20)
        await ws.prepare(request)
        sock = _HttpRelaySocket(ws)
        sock.remote = request.remote or "?"
        if not self.hub.attach(sock):
            await ws.close(code=1013, message=b"another Mac is already this Jarvis's mic")
            return ws
        self.hub.received = 0
        silence_check = asyncio.create_task(self._warn_if_silent(sock))
        try:
            await ws.send_str(json.dumps({"type": "hello", "rate": SAMPLE_RATE}))
            async for message in ws:
                if message.type == WSMsgType.BINARY:
                    self.hub.push_mic(message.data)
                elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                    break
        finally:
            silence_check.cancel()
            self.hub.detach(sock)
        return ws

    async def _warn_if_silent(self, sock: RelaySocket) -> None:
        # Like the local mic's SILENT_MIC_WARNING: a connected client with no Microphone permission sends zeros,
        # or nothing at all, so this is a timer on the first block, not a check of the sound.
        await asyncio.sleep(SILENT_CLIENT_SECS)
        if self.hub.client is sock and self.hub.received == 0:
            logger.error(
                "The relay client is connected but its mic sent nothing: allow Microphone for the terminal on that "
                "Mac (System Settings > Privacy & Security > Microphone), then restart it."
            )

    def client_script(self, fetched_from: str | None = None) -> str:
        """
        The thin client, with this Mac's address and token filled in so the laptop needs no arguments.

        `fetched_from` is the host the laptop used to download it, so a fetch over a LAN IP or a Tailscale name bakes
        in exactly that address; the advertised Bonjour name is only the fallback for the logged command.
        """
        host = self.public_host(fetched_from)
        url = f"ws://{host}:{self.port}/ws?token={self.token}"
        return CLIENT_SCRIPT.read_text(encoding="utf-8").replace(CLIENT_URL_PLACEHOLDER, f'DEFAULT_URL = "{url}"')

    def public_host(self, override: str | None = None) -> str:
        if override and override not in ("0.0.0.0", "::", "[::]"):
            return override
        hosts = advertised_hosts()
        return hosts[0] if hosts else "127.0.0.1"


def make_transport(*, port: int | None = None, token: str | None = None) -> RemoteVoiceTransport:
    """The transport `jarvis remote` puts in place of the local mic and speaker."""
    params = TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=SAMPLE_RATE,
        audio_out_sample_rate=SAMPLE_RATE,
    )
    return RemoteVoiceTransport(params, port=port, token=token)
