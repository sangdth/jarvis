#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy>=1.26", "soxr>=0.3.0", "websockets>=13.0", "pyobjc-framework-AVFoundation>=11.0"]
# ///
"""
Jarvis's thin client: this Mac's mic and speaker, nothing else.

Every model, agent and key stays on the Mac running `jarvis remote`; what runs here is a mic tap, a resampler and a
player. The mic goes through macOS voice processing (the echo canceller FaceTime uses) so Jarvis hears you over its
own voice coming out of these speakers, and so talking over it or saying "shut up" reaches it.

Get it from the Mac running Jarvis (`jarvis remote` prints the command, which fills in this file's server URL):

    curl -fsSL 'http://<mac-mini>:47210/client.py?token=<token>' -o /tmp/jarvis-remote.py
    uv run /tmp/jarvis-remote.py

Only uv (https://docs.astral.sh/uv/) is needed here: it fetches the four small packages above into a cache, not a
project. Allow the terminal app Microphone access when macOS asks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import numpy as np
import soxr
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

# Replaced with this server's own `ws://host:port/ws?token=...` in the copy `jarvis remote` serves.
DEFAULT_URL = ""

SAMPLE_RATE = 16000  # what Jarvis's pipeline runs at, both ways; this Mac's mic is resampled to it
BLOCKS = 50  # 1 s of mic audio waiting for the socket; past that the oldest goes, so talking never lags
RETRY_SECS = 2.0


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
        self._mic = soxr.ResampleStream(0, SAMPLE_RATE, 1, dtype="float32")  # rebuilt when the rate is known
        self._mic_rate = 0

    def on_mic(self) -> None:
        mic = self.engine.inputNode()
        fmt = mic.outputFormatForBus_(0)
        rate = int(fmt.sampleRate())
        self._mic_rate = rate
        self._mic = soxr.ResampleStream(rate, SAMPLE_RATE, 1, dtype="float32")

        def tap(buffer, when) -> None:  # an Objective-C void block: it must return None
            n = buffer.frameLength()
            samples = np.frombuffer(buffer.floatChannelData()[0].as_buffer(n), dtype=np.float32).copy()
            pcm = self._to_pcm(samples)
            if pcm:
                self._loop.call_soon_threadsafe(self._push, pcm)

        mic.installTapOnBus_bufferSize_format_block_(0, max(1, rate // 50), fmt, tap)  # asks for 20 ms blocks

    def _to_pcm(self, samples: np.ndarray) -> bytes:
        out = self._mic.resample_chunk(samples) if self._mic_rate != SAMPLE_RATE else samples
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
    finally:
        sender.cancel()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis's thin client: this Mac's mic and speaker for a remote Jarvis.")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help="ws://host:port/ws?token=... (usually built in)")
    parser.add_argument("--token", default="", help="the Mac's relay token, if the URL has none")
    args = parser.parse_args()
    url = args.url
    if not url:
        parser.error("no server URL: copy the command `jarvis remote` printed, which comes with one")
    if args.token:
        url += ("&" if "?" in url else "?") + f"token={args.token}"

    outbox: asyncio.Queue[bytes] = asyncio.Queue(maxsize=BLOCKS)
    audio = AudioBridge(outbox, asyncio.get_running_loop())
    audio.on_mic()
    audio.start()
    print(f"Listening with this Mac's mic and speaker. Server: {url.split('?')[0]}", flush=True)
    try:
        while True:
            try:
                async with connect(url, max_size=1 << 20) as ws:
                    await listen(ws, audio, outbox)
            except InvalidStatus as exc:
                print(f"Jarvis refused this client ({exc.response.status_code}): the token is wrong or expired.",
                      flush=True)
                return
            except (OSError, WebSocketException, TimeoutError) as exc:
                print(f"Not connected ({exc!r}); retrying in {RETRY_SECS:.0f}s. Is `jarvis remote` running?", flush=True)
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
