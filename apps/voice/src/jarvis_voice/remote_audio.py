"""
Which Mac Jarvis listens to and talks through: this one, or another one over the relay.

Jarvis runs here; the heavy half (Whisper, the speech check, the agents, the models, memory) never leaves this
Mac. Another Mac (or this one) runs one small script (`defaults/remote/client.py`) that captures its mic through
macOS voice processing and plays what comes back, so barge-in and "shut up" still work: the echo canceller has to
sit where the speaker is.

`jarvis remote` puts every ear on the other Mac. With `RELAY_EARS=true`, `jarvis` keeps this Mac's ear and adds the
relay; `Ear` follows whichever one hears you (see its docstring). A client that connects takes the ear at once.

Wire: `GET /client.py` serves that script; `GET /ws?token=...` is one WebSocket per listening Jarvis with 16-bit
mono PCM at `SAMPLE_RATE` both ways. Audio frames are binary; control is JSON (`hello`, `ear`, `flush`). The routes
are registered on the one Jarvis HTTP app (`BrainServer`), so a thin client needs one host, one port and one token.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Protocol

import numpy as np
from aiohttp import WSMsgType, web
from loguru import logger
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
    TTSAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameProcessorSetup
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

from jarvis_core.devices import DeviceStore
from jarvis_core.home import brain_token
from jarvis_core.net import advertised_hosts
from jarvis_voice.config import settings
from jarvis_voice.voice_engine import PLAY_AHEAD_SECS, MicConverter, VoiceAudio

SAMPLE_RATE = 16000  # 16-bit mono both ways, the rate the pipeline runs at; the client resamples to it
CLIENT_SCRIPT = Path(__file__).parent / "defaults" / "remote" / "client.py"
# Frames waiting for the pipeline, in 20 ms blocks: past a second behind, the oldest goes rather than lag.
MIC_QUEUE_BLOCKS = 50
# A write that takes this long means the other Mac stopped reading: drop that client, never mark the transport broken
# (Pipecat gives up on a transport whose write hangs past `audio_out_write_timeout_secs`, 10 s, for good).
SEND_TIMEOUT_SECS = 2.0
# A client that connects and sends nothing for this long has a silent mic (no Microphone permission, most likely).
SILENT_CLIENT_SECS = 3.0

LOCAL = "local"  # the Mac running Jarvis
RELAY = "relay"  # another Mac's mic and speaker, over the WebSocket
# A voice, not the quiet room the canceller leaves (-30 dBFS; a dead-silent room reads about -50). Too high and
# Jarvis ignores you on the far mic; too low and a TV elsewhere holds the ear. The [Ear] log lines say which ear.
SPEECH_PEAK = 0.03
# The ear hands over only after it has heard no speech for this long, so both mics hearing one voice (same room)
# never flips it back and forth.
HANDOVER_SECS = 1.0
# ... and the other mic needs this many speech frames in a row (60 ms), not one click.
SWITCH_FRAMES = 3
# After Jarvis's last frame plays, his own tail through the room must not count as somebody talking.
SPEAKING_GRACE_SECS = 0.5

# Where the client script reads its server from; the served copy has these replaced with this server's addresses.
CLIENT_URL_PLACEHOLDER = 'DEFAULT_URL = ""'
CLIENT_SERVER_PLACEHOLDER = 'DEFAULT_SERVER = ""'


class RelaySocket(Protocol):
    """What `RelayHub` needs from a connected client, so a test can stand in for one."""

    async def send_bytes(self, data: bytes) -> None: ...

    async def send_text(self, data: str) -> None: ...


def relay_token() -> str:
    """The token for relay clients: the one shared gateway token (`remote_token` / `REMOTE_TOKEN` can override)."""
    if settings.remote_token:
        return settings.remote_token
    return settings.brain_api_key or brain_token()


def remote_command(hosts: list[str], port: int) -> str:
    """What to paste on the other Mac: fetch the client from here, then pair it once. Only uv is needed there."""
    host = hosts[0] if hosts else "127.0.0.1"
    return (
        f"curl -fsSL 'http://{host}:{port}/client.py' -o ~/.local/bin/jarvis-remote.py"
        f" && uv run ~/.local/bin/jarvis-remote.py --pair <code from `jarvis pair`>"
    )


class Ear:
    """
    Which mic Jarvis listens to right now, and the rules for handing that over.

    `offer` runs on the event loop for both mics: the local one hops there from the audio thread.
    """

    def __init__(self, *, local: bool = True) -> None:
        self.active = LOCAL if local else RELAY
        self.frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MIC_QUEUE_BLOCKS)
        self.dropped = 0  # frames the active ear produced while the pipeline was behind
        self.on_switch: Callable[[str, str], None] | None = None
        self._online = {LOCAL: local, RELAY: False}
        self._last_heard = {LOCAL: 0.0, RELAY: 0.0}  # last speech-level frame from each mic
        self._loud = {LOCAL: 0, RELAY: 0}  # speech frames in a row, to ignore a single click
        self._speech_end = 0.0

    @property
    def local(self) -> bool:
        """Whether this Mac's own mic and speaker take part at all (`jarvis` with `RELAY_EARS`, not `remote`)."""
        return self._online[LOCAL]

    def offer(self, source: str, pcm: bytes) -> bool:
        """A mic produced `pcm`; True when it goes on to the pipeline."""
        peak = float(np.abs(np.frombuffer(pcm, dtype=np.int16)).max()) / 32768 if pcm else 0.0
        speech = peak >= SPEECH_PEAK
        self._loud[source] = self._loud[source] + 1 if speech else 0
        now = time.monotonic()
        if source == self.active:
            if speech:
                self._last_heard[source] = now
            self._put(pcm)
            return True
        if not speech or not self._online[source] or self._loud[source] < SWITCH_FRAMES:
            return False
        if now < self._speech_end + SPEAKING_GRACE_SECS:  # Jarvis is talking: nobody takes the turn
            return False
        if self._online[self.active] and now - self._last_heard[self.active] < HANDOVER_SECS:
            return False  # the ear is hearing someone too: it is where you are
        self._switch(source)
        self._last_heard[source] = now
        self._put(pcm)
        return True

    def spoke(self, seconds: float) -> None:
        """The output side just sent this much speech, on the active ear."""
        self._speech_end = max(self._speech_end, time.monotonic() + seconds)

    def set_online(self, source: str, online: bool) -> bool:
        """A mic appeared (a client connected, or this Mac's own) or went away (a client dropped).

        Returns whether the ear moved as a result, so the caller can tell the client where it now is.
        """
        self._online[source] = online
        if online:
            if source != self.active:
                self._switch(source)  # a client that connects is the Mac you picked up
                return True
            return False
        if self.active != source:
            return False
        other = LOCAL if source == RELAY else RELAY
        if self._online[other]:
            self._switch(other)
            logger.info(f"[Ear] {source} is gone; {other} has the mic again.")
            return True
        logger.warning(f"[Ear] {source} is gone and nothing else is listening.")
        return False

    def _switch(self, source: str) -> None:
        old, self.active = self.active, source
        self._loud[source] = 0
        self._last_heard[source] = time.monotonic()
        logger.info(f"[Ear] {source} has the mic now (was {old}).")
        if self.on_switch is not None:
            self.on_switch(old, source)

    def _put(self, pcm: bytes) -> None:
        if self.frames.full():
            self.frames.get_nowait()
            self.dropped += 1
        self.frames.put_nowait(pcm)


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
    """The one relay client, if any: the socket to speak into and to tell what the ear is doing."""

    def __init__(self) -> None:
        self.client: RelaySocket | None = None
        self.connected = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self.received = 0  # bytes of mic audio from the current client, to notice a silent one
        self._hint = ""

    def attach(self, ws: RelaySocket) -> bool:
        """Take the client, unless another Mac is already connected; False means refuse it."""
        if self.client is not None:
            return False
        self.client = ws
        self.received = 0
        self.connected.set()
        logger.info("Relay client connected: its mic and speaker may become Jarvis's ear.")
        return True

    def detach(self, ws: RelaySocket) -> None:
        if self.client is not ws:
            return  # a refused client, not the one in use
        self.client = None
        self.connected.clear()
        logger.info(
            "Relay client disconnected. "
            f"To listen from another Mac again: {self.reconnect_hint()}"
        )

    def reconnect_hint(self) -> str:
        """The line to paste, filled in at print time since the port is only known once it listens."""
        return self._hint

    async def send_audio(self, pcm: bytes) -> float:
        """Send speech to the client to play; returns its length in seconds to pace the pipeline."""
        client = self.client
        if client is not None and pcm:
            try:
                async with self._send_lock:  # one sender at a time: a control message must not land inside a frame
                    await asyncio.wait_for(client.send_bytes(pcm), SEND_TIMEOUT_SECS)
            except (ConnectionError, RuntimeError):
                pass  # detach() logs it; the pipeline keeps running and drops frames until a client returns
            except TimeoutError:
                logger.warning("Relay: the client stopped reading audio; dropping it until it reconnects.")
                self.detach(client)
        return len(pcm) / 2 / SAMPLE_RATE

    async def flush(self) -> None:
        """An interruption: the client drops what it has queued but not yet played."""
        await self.send_control({"type": "flush"})

    async def wait_for_client(self) -> None:
        await self.connected.wait()

    async def send_ear(self, active: bool) -> None:
        """Tell the client whether Jarvis's ear is on it, so the terminal says so."""
        await self.send_control({"type": "ear", "active": active})

    async def send_control(self, control: dict[str, Any]) -> None:
        client = self.client
        if client is None:
            return
        try:
            async with self._send_lock:
                await asyncio.wait_for(client.send_text(json.dumps(control)), SEND_TIMEOUT_SECS)
        except (ConnectionError, RuntimeError):
            pass
        except TimeoutError:
            self.detach(client)


class EarInputTransport(BaseInputTransport):
    """The active mic, as the pipeline's input: this Mac's tap, or the relay's frames, whichever `Ear` allows."""

    def __init__(self, ear: Ear, audio: VoiceAudio | None, params: TransportParams) -> None:
        super().__init__(params)
        self._ear = ear
        self._audio = audio  # None with `jarvis remote`: this Mac has no mic in the pipeline
        self._mic: MicConverter | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._silence_check: asyncio.Task[None] | None = None

    # Same signature as Pipecat's FrameProcessor.setup, which narrows BaseObject.setup the same way.
    async def setup(self, setup: FrameProcessorSetup) -> None:  # type: ignore[override]
        await super().setup(setup)
        if self._audio is None:
            return
        self._mic = MicConverter(self.sample_rate)
        self._audio.on_mic(self._on_mic)

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if self._audio is not None:
            self._audio.start()
        # Its own queue opens in set_transport_ready, so the pumping starts after it, never before.
        await self.set_transport_ready(frame)
        self._pump_task = self.create_task(self._pump())
        if self._audio is not None:
            self._silence_check = self.create_task(self._warn_if_silent())

    async def cleanup(self) -> None:
        await super().cleanup()
        for task in (self._pump_task, self._silence_check):
            if task is not None:
                await self.cancel_task(task)
        self._pump_task = self._silence_check = None
        if self._audio is not None:
            self._audio.stop()

    async def _pump(self) -> None:
        while True:
            pcm = await self._ear.frames.get()
            await self.push_audio_frame(InputAudioRawFrame(audio=pcm, sample_rate=self.sample_rate, num_channels=1))

    async def _warn_if_silent(self) -> None:
        # The tap gets no blocks at all when voice processing fails, so this is a timer, not a check on each block.
        from jarvis_voice.mac_audio import SILENT_MIC_SECS, SILENT_MIC_WARNING

        await asyncio.sleep(SILENT_MIC_SECS)
        if self._mic is not None and not self._mic.heard:
            logger.error(SILENT_MIC_WARNING)

    def _on_mic(self, samples: np.ndarray, rate: int) -> None:  # audio thread
        mic = self._mic  # set in setup, before the tap is installed
        if mic is None:
            return
        pcm = mic.convert(samples, rate)
        if self._audio is not None and self._audio.bridge is not None:
            pcm = self._audio.bridge.mix_buffer.mix(pcm)
        if pcm:
            self.get_event_loop().call_soon_threadsafe(self._ear.offer, LOCAL, pcm)


class EarOutputTransport(BaseOutputTransport):
    """Jarvis's speech, on whichever Mac has the ear: the local player or the relay client."""

    def __init__(self, ear: Ear, audio: VoiceAudio | None, hub: RelayHub, params: TransportParams) -> None:
        super().__init__(params)
        self._ear = ear
        self._audio = audio
        self._hub = hub
        self._until = 0.0  # loop time when everything sent so far has been played

    async def setup(self, setup: FrameProcessorSetup) -> None:  # type: ignore[override]  # see the input side
        await super().setup(setup)
        if self._audio is not None:
            self._audio.connect_player(self.sample_rate)

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if self._audio is not None:
            self._audio.start()
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        # Pace like a blocking speaker write, so an interruption drops the audio still in the pipeline instead of
        # everything already queued playing out.
        now = asyncio.get_running_loop().time()
        if self._ear.active == RELAY and self._hub.client is not None:
            seconds = await self._hub.send_audio(frame.audio)
        elif self._audio is not None:
            seconds = self._audio.play(frame.audio, speech=isinstance(frame, TTSAudioRawFrame))
        else:
            seconds = 0.0
        self._ear.spoke(seconds)
        self._until = max(self._until, now) + seconds
        await asyncio.sleep(max(0.0, self._until - now - PLAY_AHEAD_SECS))
        return True

    async def process_frame(self, frame: Any, direction: Any) -> None:
        if isinstance(frame, InterruptionFrame):
            await self._interrupt()
        await super().process_frame(frame, direction)

    async def _interrupt(self) -> None:
        """The user talked over Jarvis: what is still queued - here or on the other Mac - is no longer wanted."""
        self._until = 0.0
        if self._audio is not None:
            self._audio.flush()
        await self._hub.flush()


class RemoteVoiceTransport(BaseTransport):
    """
    The pipeline's audio, here plus (optionally) another Mac: this side keeps every model and every agent.

    With `local=False` (`jarvis remote`) the other Mac is the only ear. With `local=True` (`RELAY_EARS=true`) this
    Mac's mic and speaker stay in the pipeline and `Ear` hands over between them and the relay.

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
        devices: DeviceStore | None = None,
        local: bool = False,
    ) -> None:
        super().__init__()
        self._params = params
        self.token = relay_token() if token is None else token
        self.host = host or settings.brain_host
        self.port = settings.brain_port if port is None else port
        self.devices = devices  # paired devices' secrets work here too
        self.ear = Ear(local=local)
        self.ear.on_switch = self._on_switch
        self.hub = hub or RelayHub()
        self.hub._hint = remote_command(advertised_hosts(), self.port)
        # Imported here: this module is loaded for `jarvis remote` too, where no audio engine is wanted.
        from jarvis_voice.mac_audio import MacVoiceAudio

        self.audio: VoiceAudio | None = MacVoiceAudio() if local else None
        self._runner: web.AppRunner | None = None
        self._input: EarInputTransport | None = None
        self._output: EarOutputTransport | None = None
        self._pending: set[asyncio.Task[None]] = set()

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
        self.hub._hint = remote_command(advertised_hosts(), self.port)
        logger.info(f"Relay listening on {self.host}:{self.port}: the mic and speaker of another Mac reach this Jarvis.")
        logger.info(f"On that Mac (needs only uv, https://docs.astral.sh/uv/):\n  {self.hub.reconnect_hint()}")

    async def stop_server(self) -> None:
        if self._pending:
            for task in list(self._pending):
                task.cancel()
            await asyncio.gather(*self._pending, return_exceptions=True)
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
            self._input = EarInputTransport(self.ear, self.audio, self._params)
        return self._input

    def output(self) -> FrameProcessor:
        if not self._output:
            self._output = EarOutputTransport(self.ear, self.audio, self.hub, self._params)
        return self._output

    def _on_switch(self, old: str, new: str) -> None:
        """The ear moved: drop what the old Mac still had queued, and say where it is now."""
        if old == LOCAL and self.audio is not None:
            self.audio.flush()
        if old == RELAY:
            self._spawn(self.hub.flush())
        self._spawn(self.hub.send_ear(new == RELAY))

    def _spawn(self, coroutine: Any) -> None:
        # Called from `Ear.offer`, which runs on the event loop but cannot await: the switch must not be delayed.
        task = asyncio.get_running_loop().create_task(coroutine)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    @staticmethod
    def _token_from(request: web.Request) -> str:
        """The token, from `Authorization: Bearer` or `?token=`, so a browser and an iOS Shortcut both work."""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth.removeprefix("Bearer ").strip()
        return (request.query.get("token") or request.headers.get("X-Jarvis-Token") or "").strip()

    def _authorized(self, request: web.Request) -> bool:
        token = self._token_from(request)
        if not token:
            return False
        if secrets.compare_digest(token, self.token):
            return True
        # A paired device: its own secret, revocable without touching anyone else's.
        return self.devices is not None and self.devices.verify(token) is not None

    async def handle_client_script(self, request: web.Request) -> web.Response:
        """
        The thin client, as a plain HTTP file (the laptop curls it), never as a WebSocket.

        Open on purpose: it is the bootstrap, so it has to arrive before the laptop has a secret, and it carries no
        secret of its own — only this server's address. The socket it then opens still needs a pairing secret.
        """
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
            # Opening the client chooses that Mac: it takes the ear now. A switch already told it where it is
            # (through the transport); with the ear already there, it still needs to hear so.
            if not self.ear.set_online(RELAY, True):
                await self.hub.send_ear(self.ear.active == RELAY)
            async for message in ws:
                if message.type == WSMsgType.BINARY:
                    self.hub.received += len(message.data)
                    self.ear.offer(RELAY, message.data)
                elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                    break
        finally:
            silence_check.cancel()
            self.hub.detach(sock)
            self.ear.set_online(RELAY, False)
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
        The thin client, with this Mac's address filled in so the laptop needs no arguments.

        `fetched_from` is the host the laptop used to download it, so a fetch over a LAN IP or a Tailscale name bakes
        in exactly that address; the advertised Bonjour name is only the fallback for the logged command. No token is
        baked in: the laptop pairs once and keeps its own secret.
        """
        host = self.public_host(fetched_from)
        text = CLIENT_SCRIPT.read_text(encoding="utf-8")
        text = text.replace(CLIENT_URL_PLACEHOLDER, f'DEFAULT_URL = "ws://{host}:{self.port}/ws"')
        return text.replace(CLIENT_SERVER_PLACEHOLDER, f'DEFAULT_SERVER = "http://{host}:{self.port}"')

    def public_host(self, override: str | None = None) -> str:
        if override and override not in ("0.0.0.0", "::", "[::]"):
            return override
        hosts = advertised_hosts()
        return hosts[0] if hosts else "127.0.0.1"

def make_transport(
    *,
    port: int | None = None,
    token: str | None = None,
    devices: DeviceStore | None = None,
    local: bool = False,
) -> RemoteVoiceTransport:
    """The relay transport: `jarvis remote` (`local=False`) or `RELAY_EARS` (`local=True`)."""
    params = TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=SAMPLE_RATE,
        audio_out_sample_rate=SAMPLE_RATE,
    )
    return RemoteVoiceTransport(params, port=port, token=token, devices=devices, local=local)
