#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy>=1.26", "soxr>=0.3.0", "websockets>=13.0", "pyobjc-framework-AVFoundation>=11.0"]
# ///
"""
Jarvis's thin client: this Mac's mic and speaker, nothing else.

Every model, agent and key stays on the Mac running Jarvis; what runs here is a mic tap, a resampler and a
player. The mic goes through macOS voice processing (the echo canceller FaceTime uses) so Jarvis hears you over its
own voice coming out of these speakers, and so talking over it or saying "shut up" reaches it.

There is no mode to start on the other Mac: while this script runs, Jarvis hears and speaks here instead of through
its own mic and speaker, and it goes back to them when this stops.

Get it from the Mac running Jarvis (`jarvis pair` prints the command), then pair it once:

    curl -fsSL 'http://<mac-mini>:8643/client.py' -o ~/.local/bin/jarvis-remote.py
    uv run ~/.local/bin/jarvis-remote.py --pair 123456        # once, with the code Jarvis showed

The pairing code becomes a secret kept in ~/.config/jarvis-remote/credentials.json (0600), so later runs need no
arguments at all. Only uv (https://docs.astral.sh/uv/) is needed here: it fetches the four small packages above into
a cache, not a project. Allow the terminal app Microphone access when macOS asks.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
import soxr
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

# Filled in with this server's own addresses in the copy Jarvis serves. No token: the pairing secret has its place.
DEFAULT_URL = ""
DEFAULT_SERVER = ""

SAMPLE_RATE = 16000  # what Jarvis's pipeline runs at, both ways; this Mac's mic is resampled to it
BLOCKS = 50  # 1 s of mic audio waiting for the socket; past that the oldest goes, so talking never lags
RETRY_SECS = 2.0
CREDENTIALS = Path.home() / ".config" / "jarvis-remote" / "credentials.json"

# websockets renamed this argument; read the installed signature instead of guessing the version.
_HEADER_PARAM = "additional_headers" if "additional_headers" in inspect.signature(connect).parameters else "extra_headers"


def load_credentials() -> dict:
    try:
        return json.loads(CREDENTIALS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_credentials(record: dict) -> None:
    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    CREDENTIALS.parent.chmod(0o700)
    CREDENTIALS.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    CREDENTIALS.chmod(0o600)


def pair(server: str, code: str, name: str) -> dict:
    """Trade the one-time code for this Mac's own secret; the server stores only its hash."""
    body = json.dumps({"code": code, "name": name}).encode()
    request = urllib.request.Request(
        f"{server.rstrip('/')}/v1/pair", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip()
        raise SystemExit(f"Pairing failed ({exc.code}): {detail or exc.reason}") from None
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach {server}: {exc.reason}") from None
    secret = payload.get("secret")
    if not secret:
        raise SystemExit("Pairing failed: the server returned no secret.")
    device = payload.get("device") or {}
    record = {
        "server": server.rstrip("/"),
        "url": f"{server.rstrip('/')}".replace("http://", "ws://").replace("https://", "wss://") + "/ws",
        "device_id": device.get("id", ""),
        "name": device.get("name", name),
        "secret": secret,
    }
    save_credentials(record)
    print(f"Paired as {record['name']!r} ({record['device_id']}). Nothing else to set up.", flush=True)
    return record


class AudioBridge:
    """One AVAudioEngine with voice processing on: the mic tap and the player share it, so echo cancels."""

    def __init__(self, outbox: asyncio.Queue[bytes], loop: asyncio.AbstractEventLoop) -> None:
        import AVFoundation

        self._av = AVFoundation
        self._outbox = outbox
        self._loop = loop
        self.engine = AVFoundation.AVAudioEngine.alloc().init()
        # The output side must exist before voice processing is turned on, or the engine fails to start (-10875).
        self.engine.outputNode(), self.engine.mainMixerNode()
        ok, err = self.engine.inputNode().setVoiceProcessingEnabled_error_(True, None)
        if not ok:
            raise SystemExit(f"macOS voice processing could not start: {err}")
        # Voice processing turns down every other app's sound by default; keep that to the least macOS allows.
        self.engine.inputNode().setVoiceProcessingOtherAudioDuckingConfiguration_(
            (False, AVFoundation.AVAudioVoiceProcessingOtherAudioDuckingLevelMin)
        )
        self._format = AVFoundation.AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
            AVFoundation.AVAudioPCMFormatFloat32, float(SAMPLE_RATE), 1, False
        )
        self.player = AVFoundation.AVAudioPlayerNode.alloc().init()
        self.engine.attachNode_(self.player)
        self.engine.connect_to_format_(self.player, self.engine.mainMixerNode(), self._format)
        # Built in on_mic, once the mic's own rate is known: soxr refuses a stream at 0 Hz, and this Mac's mic is
        # usually 48 kHz, so the resampler cannot be a placeholder here.
        self._mic: soxr.ResampleStream | None = None

    def on_mic(self) -> None:
        mic = self.engine.inputNode()
        fmt = mic.outputFormatForBus_(0)
        rate = int(fmt.sampleRate())
        self._mic = soxr.ResampleStream(rate, SAMPLE_RATE, 1, dtype="float32") if rate != SAMPLE_RATE else None

        def tap(buffer, when) -> None:  # an Objective-C void block: it must return None
            n = buffer.frameLength()
            samples = np.frombuffer(buffer.floatChannelData()[0].as_buffer(n), dtype=np.float32).copy()
            pcm = self._to_pcm(samples)
            if pcm:
                self._loop.call_soon_threadsafe(self._push, pcm)

        mic.installTapOnBus_bufferSize_format_block_(0, max(1, rate // 50), fmt, tap)  # asks for 20 ms blocks

    def _to_pcm(self, samples: np.ndarray) -> bytes:
        out = samples if self._mic is None else self._mic.resample_chunk(samples)
        return (np.clip(out, -1.0, 1.0) * 32767).astype(np.int16).tobytes()

    def _push(self, pcm: bytes) -> None:
        if self._outbox.full():  # the socket is behind: drop the oldest rather than lag behind the speaker
            self._outbox.get_nowait()
        self._outbox.put_nowait(pcm)

    def play(self, pcm: bytes) -> None:
        """Queue 16-bit mono audio at Jarvis's rate; it plays after whatever is already queued."""
        samples = np.frombuffer(pcm, dtype=np.int16)
        if not len(samples):
            return
        buffer = self._av.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(self._format, len(samples))
        np.frombuffer(buffer.floatChannelData()[0].as_buffer(len(samples)), dtype=np.float32)[:] = samples / 32768
        buffer.setFrameLength_(len(samples))
        self.player.scheduleBuffer_completionHandler_(buffer, None)

    def flush(self) -> None:
        """Jarvis was interrupted: drop what is queued but not yet played."""
        self.player.stop()
        self.player.play()

    def start(self) -> None:
        ok, err = self.engine.startAndReturnError_(None)
        if not ok:
            raise SystemExit(f"audio engine could not start: {err}")
        self.player.play()

    def stop(self) -> None:
        self.engine.inputNode().removeTapOnBus_(0)
        self.player.stop()
        self.engine.stop()


async def send_mic(ws, outbox: asyncio.Queue[bytes]) -> None:
    while True:
        await ws.send(await outbox.get())


async def listen(ws, audio: AudioBridge, outbox: asyncio.Queue[bytes]) -> None:
    """Play Jarvis's speech until the connection ends; a `flush` drops what is queued."""
    sender = asyncio.create_task(send_mic(ws, outbox))
    try:
        async for message in ws:
            if isinstance(message, bytes):
                audio.play(message)
                continue
            control = json.loads(message)
            if control.get("type") == "flush":
                audio.flush()
            elif control.get("type") == "hello":
                print(f"Connected to Jarvis ({control.get('rate')} Hz). Talk when you like; Ctrl+C to stop.",
                      flush=True)
            elif control.get("type") == "ear":
                print("Jarvis's ear is on this Mac: talk here." if control.get("active") else
                      "Jarvis is listening on another Mac; talk here to bring the ear back.", flush=True)
    finally:
        sender.cancel()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis's thin client: this Mac's mic and speaker for a remote Jarvis.")
    parser.add_argument("url", nargs="?", default="", help="ws://host:port/ws (usually built in)")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="http://host:port of the Jarvis to pair with")
    parser.add_argument("--pair", metavar="CODE", default="", help="pair with the code `jarvis pair` showed")
    parser.add_argument("--name", default="", help="what this Mac is called in `jarvis devices`")
    parser.add_argument("--token", default="", help="the shared token, instead of a pairing secret")
    args = parser.parse_args()

    stored = load_credentials()
    server = args.server or stored.get("server") or DEFAULT_SERVER
    if args.pair:
        if not server:
            parser.error("no server to pair with: pass --server http://<mac-mini>:8643")
        stored = pair(server, args.pair, args.name or socket.gethostname().split(".")[0])
    token = args.token or stored.get("secret", "")
    url = args.url or stored.get("url") or DEFAULT_URL
    if not token:
        parser.error("this Mac is not paired: run `jarvis pair` on the other Mac, then this with --pair <code>")
    if not url:
        parser.error("no server URL: download this script again from `jarvis pair`")

    headers = {"Authorization": f"Bearer {token}"}
    connect_kwargs: dict[str, Any] = {_HEADER_PARAM: headers}
    outbox: asyncio.Queue[bytes] = asyncio.Queue(maxsize=BLOCKS)
    audio = AudioBridge(outbox, asyncio.get_running_loop())
    audio.on_mic()
    audio.start()
    print(f"Listening with this Mac's mic and speaker. Server: {url.split('?')[0]}", flush=True)
    try:
        while True:
            try:
                async with connect(url, max_size=1 << 20, **connect_kwargs) as ws:
                    await listen(ws, audio, outbox)
            except InvalidStatus as exc:
                status = exc.response.status_code
                if status in (401, 403):
                    print(f"Jarvis refused this client ({status}): this Mac's pairing was revoked.", flush=True)
                    return
                print(f"Jarvis answered {status}; retrying in {RETRY_SECS:.0f}s.", flush=True)
            except (OSError, WebSocketException, TimeoutError) as exc:
                print(f"Not connected ({exc!r}); retrying in {RETRY_SECS:.0f}s. Is Jarvis running?", flush=True)
            await asyncio.sleep(RETRY_SECS)
    except KeyboardInterrupt:
        pass
    finally:
        audio.stop()
        print("\nStopped.", flush=True)


if __name__ == "__main__":
    if sys.platform != "darwin":
        sys.exit("Jarvis's relay client uses macOS audio; it runs on a Mac.")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
