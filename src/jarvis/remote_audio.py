"""
Which Mac Jarvis listens to and talks through: this one, or another one over the relay.

Every heavy part (Whisper, the speech check, the agents, the models, memory) stays on the Mac running Jarvis. The
other Mac runs one small script (`defaults/remote/client.py`) that captures its mic through macOS voice processing
and plays what comes back, so barge-in and "shut up" still work: the echo canceller has to sit where the speaker is.

`jarvis` brings up both ears at once and `Ear` decides which one is live:

- the Mac this runs on (`LOCAL`), always on, its own mic and speaker through macOS voice processing;
- another Mac's (`RELAY`), when its client is connected, whose speech and speakers then belong to this Jarvis.

`jarvis remote` is the same thing with `local=False`: only the relayed ear, for a Mac mini nobody talks to.

Rules for handing the ear over, from `Ear`:

- a relay client that connects takes the ear at once: opening the client is you choosing that Mac;
- otherwise the ear changes when the other mic hears speech and the current ear has heard none for `HANDOVER_SECS`:
  walk from the laptop to the room and it follows you, no buttons;
- neither happens while Jarvis is speaking: another device's speaker echoing his own words must not take the turn;
- the active ear forwards everything, silence included, because voice detection needs it to end a sentence; the
  other one is dropped, not buffered.

Wire: one WebSocket per listening Jarvis, 16-bit mono PCM at `SAMPLE_RATE` both ways. Audio frames are binary, and
control is JSON (`hello`, `ear`, `flush`). The token is a shared secret in `JARVIS_HOME/remote_token` (0600) and is
required on every connection: with `MASTER_CHECK` off any voice may talk to Jarvis, and the port is on the LAN.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import time
from email.utils import formatdate
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs

import numpy as np
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
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

from jarvis.config import JARVIS_HOME, settings
from jarvis.voice_engine import PLAY_AHEAD_SECS, SILENT_MIC_SECS, SILENT_MIC_WARNING, MicConverter, VoiceAudio

SAMPLE_RATE = 16000  # 16-bit mono both ways, the rate the pipeline runs at; a client resamples to it
PORT = 47210  # also in the printed client command; 0 in REMOTE_PORT turns the relay off
TOKEN_PATH = JARVIS_HOME / "remote_token"
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
# A voice, not the quiet room the canceller leaves (-30 dBFS; a dead-silent room reads about -50). Too high and Jarvis
# ignores you on the far mic; too low and a TV elsewhere holds the ear. The [Ear] log lines say which ear heard you.
SPEECH_PEAK = 0.03
# The ear hands over only after it has heard no speech for this long, so both mics hearing one voice (same room)
# never flips it back and forth.
HANDOVER_SECS = 1.0
# ... and the other mic needs this many speech frames in a row (60 ms), not one click.
SWITCH_FRAMES = 3
# After Jarvis's last frame plays, his own tail through the room must not count as somebody talking.
SPEAKING_GRACE_SECS = 0.5

# The served copy of the client has this replaced with this server's own URL.
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


def remote_command(hosts: list[str], token: str, port: int = PORT) -> str:
    """What to paste on the other Mac: fetch the client from here and run it. Only uv is needed there."""
    host = hosts[0] if hosts else "127.0.0.1"
    return (
        f"curl -fsSL 'http://{host}:{port}/client.py?token={token}' -o /tmp/jarvis-remote.py"
        f" && uv run /tmp/jarvis-remote.py"
    )


class Ear:
    """
    Which mic Jarvis listens to right now, and the rules for handing that over (see the module docstring).

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
        """Whether this Mac's own mic and speaker take part at all (`jarvis` yes, `jarvis remote` no)."""
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


class RelayHub:
    """The one relay client, if any: the socket to speak into and to tell what the ear is doing."""

    def __init__(self, token: str = "", port: int = PORT) -> None:
        self.token, self.port = token, port  # only to print the command to reconnect
        self.client: ServerConnection | None = None
        self.connected = asyncio.Event()
        self.received = 0  # bytes of mic audio from the current client, to notice a silent one
        self._send_lock = asyncio.Lock()

    def attach(self, ws: ServerConnection) -> bool:
        """Take the client, unless another Mac is already connected; False means refuse it."""
        if self.client is not None:
            return False
        self.client = ws
        self.received = 0
        self.connected.set()
        return True

    def detach(self, ws: ServerConnection) -> None:
        if self.client is not ws:
            return  # a refused client, not the one in use
        self.client = None
        self.connected.clear()
        logger.info(
            "Relay client disconnected. "
            f"To listen from another Mac again: {remote_command(advertised_hosts(), self.token, self.port)}"
        )

    async def send_audio(self, pcm: bytes) -> float:
        """Send speech to the client to play; returns its length in seconds to pace the pipeline."""
        client = self.client
        if client is not None and pcm:
            try:
                async with self._send_lock:  # one sender at a time: a control message must not land inside a frame
                    await asyncio.wait_for(client.send(pcm), SEND_TIMEOUT_SECS)
            except ConnectionClosed:
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
                await asyncio.wait_for(client.send(json.dumps(control)), SEND_TIMEOUT_SECS)
        except ConnectionClosed:
            pass
        except TimeoutError:
            self.detach(client)

    def command(self) -> str:
        return remote_command(advertised_hosts(), self.token, self.port)


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
        await asyncio.sleep(SILENT_MIC_SECS)
        if self._mic is not None and not self._mic.heard:
            logger.error(SILENT_MIC_WARNING)

    def _on_mic(self, samples: np.ndarray, rate: int) -> None:  # audio thread
        mic = self._mic  # set in setup, before the tap is installed
        if mic is None:
            return
        pcm = mic.convert(samples, rate)
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

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
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


class RelayServer:
    """The WebSocket the other Mac connects to: one client, its token, and the client script it downloads."""

    def __init__(self, ear: Ear, hub: RelayHub, *, port: int = PORT) -> None:
        self.ear = ear
        self.hub = hub
        self.port = port
        self._server: Server | None = None

    async def start(self) -> None:
        self._server = await serve(self._handle, "0.0.0.0", self.port, process_request=self._process_request,
                                   max_size=1 << 20)
        self.port = self.hub.port = self._server.sockets[0].getsockname()[1]
        logger.info(f"Relay listening on port {self.port}: another Mac's mic and speaker may become Jarvis's ear.")
        logger.info(f"On that Mac (needs only uv, https://docs.astral.sh/uv/):\n  {self.hub.command()}")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _process_request(self, connection: ServerConnection, request: Any) -> Response | None:
        """Serve the client script and refuse a connection without this Mac's token; both are behind the token."""
        path, _, query = request.path.partition("?")
        if not secrets.compare_digest(parse_qs(query).get("token", [""])[0], self.hub.token):
            logger.warning(f"Relay: refused a connection from {connection.remote_address[0]} (bad token).")
            return connection.respond(401, "Jarvis relay: the token does not match this Mac's.\n")
        if path == "/client.py":
            return self._script_response()
        if path == "/ws":
            return None  # the WebSocket the client speaks on
        return connection.respond(404, "Jarvis relay: /client.py and /ws, nothing else.\n")

    def _script_response(self) -> Response:
        """The client script as a plain HTTP file (the other Mac curls it), not a WebSocket."""
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
        """The thin client, with this Mac's address and token filled in so the other Mac needs no arguments."""
        url = f"ws://{self.public_host()}:{self.port}/ws?token={self.hub.token}"
        return CLIENT_SCRIPT.read_text().replace(CLIENT_URL_PLACEHOLDER, f'DEFAULT_URL = "{url}"')

    def public_host(self) -> str:
        hosts = advertised_hosts()
        return hosts[0] if hosts else "127.0.0.1"

    async def _handle(self, ws: ServerConnection) -> None:
        if not self.hub.attach(ws):
            await ws.close(code=1013, reason="another Mac is already connected")
            return
        silence_check = asyncio.create_task(self._warn_if_silent(ws))
        try:
            await ws.send(json.dumps({"type": "hello", "rate": SAMPLE_RATE}))
            # Opening the client chooses that Mac: it takes the ear now. A switch already told it where it is
            # (through the transport); with the ear already there, it still needs to hear so.
            if not self.ear.set_online(RELAY, True):
                await self.hub.send_ear(self.ear.active == RELAY)
            async for message in ws:
                if isinstance(message, bytes):
                    self.hub.received += len(message)
                    self.ear.offer(RELAY, message)
        except ConnectionClosed:
            pass
        finally:
            silence_check.cancel()
            self.hub.detach(ws)
            self.ear.set_online(RELAY, False)

    async def _warn_if_silent(self, ws: ServerConnection) -> None:
        # A connected client with no Microphone permission sends zeros, or nothing at all, so this is a timer on the
        # first block, not a check of the sound.
        await asyncio.sleep(SILENT_CLIENT_SECS)
        if self.hub.client is ws and self.hub.received == 0:
            logger.error(
                "The relay client is connected but its mic sent nothing: allow Microphone for the terminal on that "
                "Mac (System Settings > Privacy & Security > Microphone), then restart it."
            )


class HybridVoiceTransport(BaseTransport):
    """This Mac's mic and speaker, plus another Mac's over the relay; `Ear` says which is live."""

    def __init__(self, params: TransportParams, *, port: int = PORT, local: bool = True,
                 token: str | None = None) -> None:
        super().__init__()
        self._params = params
        self.ear = Ear(local=local)
        self.ear.on_switch = self._on_switch
        self.hub = RelayHub(relay_token() if token is None else token, port)
        self.server = RelayServer(self.ear, self.hub, port=port) if port else None
        # Imported here: this module is loaded for `jarvis remote` too, where no audio engine is wanted.
        from jarvis.mac_audio import MacVoiceAudio

        self.audio: VoiceAudio | None = MacVoiceAudio() if local else None
        self._input: EarInputTransport | None = None
        self._output: EarOutputTransport | None = None
        self._pending: set[asyncio.Task[None]] = set()

    async def start_server(self) -> None:
        if self.server is not None:
            await self.server.start()

    async def stop_server(self) -> None:
        if self.server is not None:
            await self.server.stop()

    async def wait_for_client(self) -> None:
        await self.hub.connected.wait()

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


def make_transport(port: int = PORT, *, local: bool = True) -> HybridVoiceTransport:
    """`jarvis`'s transport when the relay is on (`local=True`), and `jarvis remote`'s (`local=False`)."""
    params = TransportParams(audio_in_enabled=True, audio_out_enabled=True, audio_in_sample_rate=SAMPLE_RATE,
                             audio_out_sample_rate=SAMPLE_RATE)
    return HybridVoiceTransport(params, port=port, local=local)
