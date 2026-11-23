"""
The thin client's mic and speaker, on the same HTTP server as everything else.

Jarvis runs here; the heavy half (Whisper, the speech check, the agents, the models, memory) never leaves this
Mac. Another Mac (or this one) runs one small script (`defaults/remote/client.py`) that captures its mic through
macOS voice processing and plays what comes back, so barge-in and "shut up" still work: the echo canceller has to
sit where the speaker is.

The routes are always mounted: while a paired Mac is connected it owns the mic and speaker, and this Mac's own
hardware takes them back when it goes. There is no `jarvis remote` mode to switch into.

Wire: `GET /client.py` serves that script; `GET /ws?token=...` is one WebSocket per listening Jarvis with 16-bit
mono PCM at `SAMPLE_RATE` both ways (local speech is resampled to it). Audio frames are binary; control is JSON
(`hello`, `flush`). The routes are registered on the one Jarvis HTTP app (`BrainServer`), so a thin client needs
one host, one port and one token.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import soxr
from aiohttp import WSMsgType, web
from loguru import logger

from jarvis_core.devices import DeviceStore
from jarvis_core.home import REPO_ROOT, brain_token
from jarvis_core.net import advertised_hosts
from jarvis_voice.config import settings

SAMPLE_RATE = 16000  # 16-bit mono both ways, the rate the pipeline runs at; the client resamples to it
CLIENT_SCRIPT = Path(__file__).parent / "defaults" / "remote" / "client.py"
# The Connect app, built by `apps/connect/build.sh` on this checkout and served like the client script: no secret
# inside.
MAC_APP_ZIP = REPO_ROOT / "target" / "connect" / "Jarvis.app.zip"
MAC_APP_NAME = "Jarvis.app"
# Mic audio waiting for the pipeline, in 20 ms blocks: past a second behind, the oldest goes rather than lag.
MIC_QUEUE_BLOCKS = 50
# A write that takes this long means the other Mac stopped reading: drop that client, never mark the transport broken
# (Pipecat gives up on a transport whose write hangs past `audio_out_write_timeout_secs`, 10 s, for good).
SEND_TIMEOUT_SECS = 2.0
# A client that connects and sends nothing for this long has a silent mic (no Microphone permission, most likely).
SILENT_CLIENT_SECS = 3.0

LOCAL = "local"  # the Mac running Jarvis
RELAY = "relay"  # a paired Mac's mic and speaker, over the WebSocket
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

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


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

    A paired Mac that connects takes the ear at once; while both are connected the ear moves to whichever mic
    hears speech after the other has been quiet for `HANDOVER_SECS`. `accept` runs on the event loop for both
    mics: the local one hops there from the audio thread.
    """

    def __init__(self, *, local: bool = False) -> None:
        self.active = LOCAL if local else RELAY
        self.on_switch: Callable[[str, str], None] | None = None
        self._online = {LOCAL: local, RELAY: False}
        self._last_heard = {LOCAL: 0.0, RELAY: 0.0}  # last speech-level frame from each mic
        self._loud = {LOCAL: 0, RELAY: 0}  # speech frames in a row, to ignore a single click
        self._speech_end = 0.0

    @property
    def local(self) -> bool:
        """Whether this Mac's own mic and speaker take part at all (a headless Mac mini has none)."""
        return self._online[LOCAL]

    def set_local(self, available: bool) -> None:
        """This Mac's audio appeared (or never existed); the ear starts here when nothing else is online."""
        self._online[LOCAL] = available
        if not available and self.active == LOCAL:
            self._switch(RELAY)
        elif available and self.active == RELAY and not self._online[RELAY]:
            self._switch(LOCAL)

    def accept(self, source: str, pcm: bytes) -> bool:
        """A mic produced `pcm`; True when it goes on to the pipeline."""
        peak = float(np.abs(np.frombuffer(pcm, dtype=np.int16)).max()) / 32768 if pcm else 0.0
        speech = peak >= SPEECH_PEAK
        self._loud[source] = self._loud[source] + 1 if speech else 0
        now = time.monotonic()
        if source == self.active:
            if speech:
                self._last_heard[source] = now
            return True
        if not speech or not self._online[source] or self._loud[source] < SWITCH_FRAMES:
            return False
        if now < self._speech_end + SPEAKING_GRACE_SECS:  # Jarvis is talking: nobody takes the turn
            return False
        if self._online[self.active] and now - self._last_heard[self.active] < HANDOVER_SECS:
            return False  # the ear is hearing someone too: it is where you are
        self._switch(source)
        self._last_heard[source] = now
        return True

    def spoke(self, seconds: float) -> None:
        """The output side just sent this much speech, on the active ear."""
        self._speech_end = max(self._speech_end, time.monotonic() + seconds)

    def set_online(self, source: str, online: bool) -> bool:
        """A mic appeared (a client connected) or went away (a client dropped).

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


class _HttpRelaySocket:
    """Adapts one aiohttp WebSocket to what `RelayHub` and `send_audio` speak."""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self.remote = "?"
        self.device_id: str | None = None  # set when a paired device's own secret opened it

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
        # The local mic and speaker hand over while a client is connected, and take over again when it goes.
        self.on_attach: list[Callable[[], None]] = []
        self.on_detach: list[Callable[[], None]] = []
        self._resampler: soxr.ResampleStream | None = None
        self._resampler_rate = 0

    def attach(self, ws: RelaySocket) -> bool:
        """Take the client, unless another Mac is already the mic; False means refuse it."""
        if self.client is not None:
            return False
        self.client = ws
        self._drain_mic()  # what the previous client left is no longer wanted
        self.connected.set()
        for callback in self.on_attach:
            try:
                callback()
            except Exception as exc:  # a handoff failure must not cost the client its connection
                logger.warning(f"Relay: the local audio could not hand over ({exc!r}).")
        logger.info("Relay client connected: hearing its mic and speaking into its speakers.")
        return True

    def detach(self, ws: RelaySocket) -> None:
        if self.client is not ws:
            return  # a refused or replaced client, not the one in use
        self.client = None
        self._drain_mic()
        self.connected.clear()
        self._reset_resampler()
        for callback in self.on_detach:
            try:
                callback()
            except Exception as exc:
                logger.warning(f"Relay: the local audio could not take over again ({exc!r}).")
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

    async def send_audio(self, pcm: bytes, *, sample_rate: int | None = None) -> float:
        """Send speech to the client to play; returns its length in seconds to pace the pipeline."""
        rate = sample_rate or SAMPLE_RATE
        seconds = len(pcm) / 2 / rate
        client = self.client
        if client is not None and pcm:
            payload = self._at_relay_rate(pcm, rate)
            try:
                async with self._send_lock:  # one sender at a time: a flush must not land inside a frame
                    await asyncio.wait_for(client.send_bytes(payload), SEND_TIMEOUT_SECS)
            except (ConnectionError, RuntimeError):
                pass  # detach() logs it; the pipeline keeps running and drops frames until a client returns
            except TimeoutError:
                logger.warning("Relay: the client stopped reading audio; dropping it until it reconnects.")
                self.detach(client)
        return seconds

    def _at_relay_rate(self, pcm: bytes, rate: int) -> bytes:
        """16-bit PCM at `rate` as the 16 kHz stream the client plays; local speech is 24 kHz, the client is not."""
        if rate == SAMPLE_RATE:
            return pcm
        if self._resampler is None or self._resampler_rate != rate:
            self._resampler = soxr.ResampleStream(rate, SAMPLE_RATE, 1, dtype="int16")
            self._resampler_rate = rate
        return self._resampler.resample_chunk(np.frombuffer(pcm, dtype=np.int16)).tobytes()

    def _reset_resampler(self) -> None:
        self._resampler = None
        self._resampler_rate = 0

    async def flush(self) -> None:
        """An interruption: the client drops what it has queued but not yet played."""
        self._reset_resampler()  # the audio it held belongs to the sentence that was cut off
        await self.send_control({"type": "flush"})

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

    async def wait_for_client(self) -> None:
        await self.connected.wait()

    _hint = ""


class RemoteVoiceTransport:
    """
    The thin client's routes: this side keeps every model and every agent.

    The two routes (`/ws` and `/client.py`) are registered on the one Jarvis HTTP app; `web_app()` builds a
    standalone app for the voice-only case and the tests. The mic and speaker themselves belong to
    `MacVoiceTransport`, which shares this transport's `ear` and `hub`.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        hub: RelayHub | None = None,
        host: str | None = None,
        port: int | None = None,
        devices: DeviceStore | None = None,
    ) -> None:
        self.token = relay_token() if token is None else token
        self.host = host or settings.brain_host
        self.port = settings.brain_port if port is None else port
        self.devices = devices  # paired devices' secrets work here too
        if devices is not None:
            devices.on_revoke(self._device_revoked)  # a revoked Mac loses its live mic at once
        self.ear = Ear(local=False)
        self.hub = hub or RelayHub()
        self.hub._hint = remote_command(advertised_hosts(), self.port)
        self._runner: web.AppRunner | None = None

    def routes(self) -> list[web.RouteDef]:
        """The relay's routes, to register on the shared Jarvis app: the script, the app, and the socket."""
        return [
            web.get("/client.py", self.handle_client_script),
            web.get("/Jarvis.app.zip", self.handle_app_zip),
            web.get("/install-app", self.handle_install_script),
            web.get("/ws", self.handle_ws),
        ]

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
        logger.info(f"Thin client listening on {self.host}:{self.port}: another Mac's mic and speaker reach this Jarvis.")
        logger.info(f"Pair one (`jarvis pair`); on that Mac (needs only uv, https://docs.astral.sh/uv/):\n"
                    f"  {self.hub.reconnect_hint()}")

    async def stop_server(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def announce(self) -> None:
        """Log how a paired device connects, once the shared app knows the real port."""
        logger.info(
            f"Thin client on {self.public_host()}:{self.port}: a paired Mac's mic and speaker reach this Jarvis, "
            "and take over from this Mac's while connected."
        )
        logger.info(f"Pair one with `jarvis pair`; on that Mac (needs only uv, https://docs.astral.sh/uv/):\n"
                    f"  {self.hub.reconnect_hint()}")

    async def wait_for_client(self) -> None:
        await self.hub.wait_for_client()

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

    def _device_for(self, request: web.Request) -> str | None:
        """The paired device this request's secret belongs to, so a revocation can find its socket."""
        if self.devices is None:
            return None
        device = self.devices.verify(self._token_from(request))
        return device.id if device is not None else None

    def _device_revoked(self, device_id: str) -> None:
        """`jarvis devices revoke`: drop that Mac's live socket now instead of when its network goes."""
        client = self.hub.client
        if client is None or getattr(client, "device_id", None) != device_id:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no socket can be live without a running loop
        loop.create_task(client.close(1008, "this device was revoked"))
        logger.info(f"Relay: cut off the revoked device {device_id}.")

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

    async def handle_app_zip(self, request: web.Request) -> web.StreamResponse:
        """
        The mac app itself, built on this checkout, as a plain `.app.zip`.

        Open like `/client.py`, and for the same reason: it arrives before the client has a secret and carries none.
        The bundle is signed as it ships, so nothing is patched per request.
        """
        if not MAC_APP_ZIP.is_file():
            return web.Response(
                status=404,
                text="No mac app built on this checkout yet: run `jarvis start` (or `just build`) on the Mac "
                "running Jarvis, then download again.\n",
            )
        return web.FileResponse(
            MAC_APP_ZIP,
            headers={
                "Content-Disposition": f'attachment; filename="{MAC_APP_NAME}.zip"',
                "Cache-Control": "no-store",
            },
        )

    async def handle_install_script(self, request: web.Request) -> web.Response:
        """`curl -fsSL http://<mac-mini>:8643/install-app | sh`: install into /Applications and open it."""
        body = self.install_script(request.url.host)
        return web.Response(
            text=body,
            content_type="text/x-shellscript",
            charset="utf-8",
            headers={"Cache-Control": "no-store", "Content-Disposition": 'inline; filename="install-app.sh"'},
        )

    def install_script(self, fetched_from: str | None = None) -> str:
        """The installer, with this Mac's address baked in; only `server` is written, never a secret."""
        base = f"http://{self.public_host(fetched_from)}:{self.port}"
        return f"""#!/bin/sh
# Installs the Jarvis mac app from {base}, then opens it.
# No credential is written here: the app asks for a pairing code, and keeps its own secret after that.
set -eu
if [ "$(uname -s)" != "Darwin" ]; then
  echo "The Jarvis app runs on a Mac." >&2
  exit 1
fi
TARGET="$HOME/Applications"
if [ -d /Applications ] && [ -w /Applications ]; then TARGET=/Applications; fi
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "Downloading {MAC_APP_NAME} from {base}..."
curl -fsSL '{base}/Jarvis.app.zip' -o "$TMP/app.zip"
ditto -xk "$TMP/app.zip" "$TMP/out"
mkdir -p "$TARGET"
rm -rf "$TARGET/{MAC_APP_NAME}"
ditto "$TMP/out/{MAC_APP_NAME}" "$TARGET/{MAC_APP_NAME}"
xattr -dr com.apple.quarantine "$TARGET/{MAC_APP_NAME}" 2>/dev/null || true
mkdir -p "$HOME/.config/jarvis-remote"
chmod 700 "$HOME/.config/jarvis-remote"
printf '%s\\n' '{base}' > "$HOME/.config/jarvis-remote/server"
chmod 600 "$HOME/.config/jarvis-remote/server"
open "$TARGET/{MAC_APP_NAME}"
echo "Installed $TARGET/{MAC_APP_NAME}; it points at {base}."
"""

    async def handle_ws(self, request: web.Request) -> web.StreamResponse:
        """One WebSocket per listening Jarvis: binary audio down, binary audio up, JSON control both ways."""
        if not self._authorized(request):
            logger.warning(f"Relay: refused a connection from {request.remote} (bad token).")
            return web.Response(status=401, text="Jarvis relay: the token does not match this Mac's.\n")
        ws = web.WebSocketResponse(max_msg_size=1 << 20)
        await ws.prepare(request)
        sock = _HttpRelaySocket(ws)
        sock.remote = request.remote or "?"
        sock.device_id = self._device_for(request)
        if not self.hub.attach(sock):
            await ws.close(code=1013, message=b"another Mac is already this Jarvis's mic")
            return ws
        self.hub.received = 0
        silence_check = asyncio.create_task(self._warn_if_silent(sock))
        try:
            await ws.send_str(json.dumps({"type": "hello", "rate": SAMPLE_RATE}))
            # Opening the client chooses that Mac: it takes the ear now. A speech switch already told it where it
            # is (through the transport); with the ear already there, it still needs to hear so.
            if not self.ear.set_online(RELAY, True):
                await self.hub.send_ear(self.ear.active == RELAY)
            async for message in ws:
                if message.type == WSMsgType.BINARY:
                    self.hub.push_mic(message.data)
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
    *, port: int | None = None, token: str | None = None, devices: DeviceStore | None = None
) -> RemoteVoiceTransport:
    """The relay that a paired Mac's mic and speaker connect to (always mounted on the Jarvis server)."""
    return RemoteVoiceTransport(port=port, token=token, devices=devices)
