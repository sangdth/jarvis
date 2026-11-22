import asyncio
import json
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from pipecat.frames.frames import OutputAudioRawFrame
from pipecat.transports.base_transport import TransportParams
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from jarvis import remote_audio
from jarvis.remote_audio import (
    LOCAL,
    RELAY,
    SAMPLE_RATE,
    Ear,
    EarOutputTransport,
    RelayHub,
    RelayServer,
    relay_token,
)


def frame(peak: float, samples: int = 320) -> bytes:
    """One 20 ms mic frame peaking at `peak` of full scale."""
    return np.full(samples, int(peak * 32767), dtype=np.int16).tobytes()


def silence(samples: int = 320) -> bytes:
    return bytes(samples * 2)


class FakeClock:
    """`time.monotonic()` the test moves by hand, so the handover rules are checked without waiting."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class FakeConnection:
    """A relay client's socket, recording what Jarvis sends it."""

    def __init__(self) -> None:
        self.sent: list[bytes | str] = []

    async def send(self, message: bytes | str) -> None:
        self.sent.append(message)


class TestEar(unittest.IsolatedAsyncioTestCase):
    """Whoever speaks wins, with the rules that make that stable in a real room."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.patch = mock.patch.object(remote_audio, "time", self.clock)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.ear = Ear()

    def test_the_mac_it_runs_on_listens_first(self) -> None:
        self.assertEqual(self.ear.active, LOCAL)
        self.assertTrue(self.ear.local)

    async def test_the_active_ear_forwards_silence_too(self) -> None:
        # Voice detection needs the quiet frames: without them a sentence never ends.
        self.assertTrue(self.ear.offer(LOCAL, silence()))
        self.assertTrue(self.ear.offer(LOCAL, frame(0.2)))
        self.assertEqual(self.ear.frames.qsize(), 2)

    async def test_the_quiet_mic_is_dropped_not_buffered(self) -> None:
        self.ear.set_online(RELAY, True)  # connects: takes the ear
        self.ear._switch(LOCAL)  # ... and you are talking here, so this Mac has it back
        self.assertFalse(self.ear.offer(RELAY, silence()))
        self.assertEqual(self.ear.frames.qsize(), 0)

    async def test_a_client_that_connects_takes_the_ear_at_once(self) -> None:
        self.ear.offer(LOCAL, frame(0.3))  # you were talking here a moment ago
        self.ear.set_online(RELAY, True)
        self.assertEqual(self.ear.active, RELAY)

    async def test_speech_on_the_quiet_mic_takes_the_ear_after_a_silence(self) -> None:
        self.ear.set_online(RELAY, True)  # a client is connected ...
        self.ear._switch(LOCAL)  # ... but you are at this Mac, talking here
        self.clock.now += 10
        self.ear.offer(LOCAL, frame(0.3))
        for _ in range(remote_audio.SWITCH_FRAMES):
            self.assertFalse(self.ear.offer(RELAY, frame(0.2)))  # not yet: this Mac is hearing you too
        self.assertEqual(self.ear.active, LOCAL)
        self.clock.now += remote_audio.HANDOVER_SECS  # you walked away and it went quiet here
        self.assertTrue(self.ear.offer(RELAY, frame(0.2)))
        self.assertEqual(self.ear.active, RELAY)

    async def test_one_click_does_not_take_the_ear(self) -> None:
        self.ear.set_online(RELAY, True)
        self.ear._switch(LOCAL)  # pretend the relay is quiet for a while
        self.clock.now += 10
        for _ in range(remote_audio.SWITCH_FRAMES - 1):
            self.ear.offer(RELAY, frame(0.5))
        self.ear.offer(RELAY, silence())  # the click ends
        self.ear.offer(RELAY, frame(0.5))  # and a new one starts: still not a voice
        self.assertEqual(self.ear.active, LOCAL)

    async def test_nothing_takes_the_turn_while_jarvis_speaks(self) -> None:
        self.ear.set_online(RELAY, True)
        self.ear._switch(LOCAL)
        self.clock.now += 10
        self.ear.spoke(2.0)  # a long reply, playing here
        for _ in range(remote_audio.SWITCH_FRAMES):
            self.assertFalse(self.ear.offer(RELAY, frame(0.4)))  # the other Mac hears this speaker
        self.assertEqual(self.ear.active, LOCAL)
        self.clock.now += 3.0  # reply over, plus the grace period
        for _ in range(remote_audio.SWITCH_FRAMES):
            self.ear.offer(RELAY, frame(0.4))
        self.assertEqual(self.ear.active, RELAY)

    async def test_a_client_that_leaves_gives_the_ear_back(self) -> None:
        self.ear.set_online(RELAY, True)
        self.assertEqual(self.ear.active, RELAY)
        self.ear.set_online(RELAY, False)
        self.assertEqual(self.ear.active, LOCAL)

    async def test_a_switch_tells_the_transport(self) -> None:
        moved: list[tuple[str, str]] = []
        self.ear.on_switch = lambda old, new: moved.append((old, new))
        self.ear.set_online(RELAY, True)
        self.assertEqual(moved, [(LOCAL, RELAY)])

    async def test_relay_only_has_no_local_ear(self) -> None:
        ear = Ear(local=False)
        self.assertEqual(ear.active, RELAY)
        self.assertFalse(ear.local)
        self.assertFalse(ear.offer(LOCAL, frame(0.5)))  # nothing to hear this Mac's mic with


class TestRelayHub(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hub = RelayHub()

    def test_one_client_at_a_time(self) -> None:
        first, second = FakeConnection(), FakeConnection()
        self.assertTrue(self.hub.attach(first))
        self.assertFalse(self.hub.attach(second))
        self.assertIs(self.hub.client, first)
        self.assertTrue(self.hub.connected.is_set())

    def test_detaching_a_refused_client_leaves_the_real_one_alone(self) -> None:
        first, second = FakeConnection(), FakeConnection()
        self.hub.attach(first)
        self.hub.attach(second)
        self.hub.detach(second)
        self.assertIs(self.hub.client, first)
        self.hub.detach(first)
        self.assertIsNone(self.hub.client)
        self.assertFalse(self.hub.connected.is_set())

    async def test_audio_with_no_client_is_dropped_but_still_timed(self) -> None:
        pcm = bytes(SAMPLE_RATE // 2)  # 0.25 s of 16-bit mono
        self.assertAlmostEqual(await self.hub.send_audio(pcm), 0.25, places=3)
        await self.hub.flush()  # nothing to flush, and no error

    async def test_audio_and_control_reach_the_client(self) -> None:
        client = FakeConnection()
        self.hub.attach(client)
        pcm = bytes(SAMPLE_RATE * 2 // 10)  # 0.1 s of 16-bit mono
        self.assertAlmostEqual(await self.hub.send_audio(pcm), 0.1, places=3)
        await self.hub.flush()
        await self.hub.send_ear(True)
        self.assertEqual(
            client.sent,
            [pcm, json.dumps({"type": "flush"}), json.dumps({"type": "ear", "active": True})],
        )

    async def test_wait_for_client_returns_when_one_connects(self) -> None:
        waiter = asyncio.create_task(self.hub.wait_for_client())
        await asyncio.sleep(0)  # let it start and block
        self.assertFalse(waiter.done())
        self.hub.attach(FakeConnection())
        await asyncio.wait_for(waiter, 1)

    async def test_a_client_that_stops_reading_is_dropped_not_the_transport(self) -> None:
        class Stalled(FakeConnection):
            async def send(self, message) -> None:
                await asyncio.sleep(60)  # a Wi-Fi link that went quiet with the socket still up

        stalled = Stalled()
        self.hub.attach(stalled)
        with mock.patch.object(remote_audio, "SEND_TIMEOUT_SECS", 0.01):
            await self.hub.send_audio(bytes(320))
        self.assertIsNone(self.hub.client)  # gone, so the next Mac can take the ear


class FakeAudio:
    """The local engine, stood in for by a recorder."""

    def __init__(self) -> None:
        self.played: list[bytes] = []
        self.flushes = 0

    def play(self, pcm: bytes, *, speech: bool = True) -> float:
        self.played.append(pcm)
        return len(pcm) / 2 / SAMPLE_RATE

    def flush(self) -> None:
        self.flushes += 1


class TestEarOutput(unittest.IsolatedAsyncioTestCase):
    def _transport(self, *, local: bool = True) -> tuple[EarOutputTransport, Ear, FakeAudio | None, FakeConnection]:
        ear = Ear(local=local)
        hub = RelayHub()
        client = FakeConnection()
        hub.attach(client)  # connected, whether or not it has the ear: that is `set_online`'s job
        audio = FakeAudio() if local else None
        params = TransportParams(audio_out_enabled=True, audio_out_sample_rate=SAMPLE_RATE)
        return EarOutputTransport(ear, audio, hub, params), ear, audio, client

    async def test_speech_goes_to_this_macs_speaker_while_it_has_the_ear(self) -> None:
        transport, ear, audio, _ = self._transport()
        pcm = bytes(SAMPLE_RATE * 2 // 10)  # 0.1 s
        with mock.patch.object(remote_audio.asyncio, "sleep", new=mock.AsyncMock()) as sleep:
            self.assertTrue(await transport.write_audio_frame(
                OutputAudioRawFrame(audio=pcm, sample_rate=SAMPLE_RATE, num_channels=1)))
        self.assertEqual(audio.played, [pcm])
        sleep.assert_awaited_once()  # paced, like a blocking speaker write
        self.assertGreater(ear._speech_end, 0.0)  # recorded as Jarvis speaking, so no mic steals the turn

    async def test_speech_goes_over_the_relay_when_that_mac_has_the_ear(self) -> None:
        transport, ear, audio, client = self._transport()
        ear.set_online(RELAY, True)
        pcm = bytes(640)
        with mock.patch.object(remote_audio.asyncio, "sleep", new=mock.AsyncMock()):
            await transport.write_audio_frame(OutputAudioRawFrame(audio=pcm, sample_rate=SAMPLE_RATE, num_channels=1))
        self.assertEqual(client.sent, [pcm])
        self.assertEqual(audio.played, [])

    async def test_an_interruption_flushes_wherever_the_sound_was_queued(self) -> None:
        transport, ear, audio, client = self._transport()
        ear.set_online(RELAY, True)
        transport._until = 99.0  # as if a long reply were playing
        await transport._interrupt()
        self.assertEqual(transport._until, 0.0)
        self.assertIn(json.dumps({"type": "flush"}), client.sent)
        self.assertEqual(audio.flushes, 1)  # the local player is cleared too: the ear may come back

    async def test_relay_only_plays_nothing_when_the_client_is_gone(self) -> None:
        transport, ear, audio, client = self._transport(local=False)
        await transport._hub.flush()  # a live client, so this is not the "no client" path
        transport._hub.client = None  # dropped between frames
        with mock.patch.object(remote_audio.asyncio, "sleep", new=mock.AsyncMock()):
            self.assertTrue(await transport.write_audio_frame(
                OutputAudioRawFrame(audio=bytes(640), sample_rate=SAMPLE_RATE, num_channels=1)))
        self.assertIsNone(audio)


class TestEarInput(unittest.IsolatedAsyncioTestCase):
    """The active ear's frames, through a real pipeline: the base input queue, the VAD passthrough, the works."""

    async def test_the_ears_frames_reach_the_pipeline(self) -> None:
        from pipecat.frames.frames import EndFrame, InputAudioRawFrame
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.runner import PipelineRunner
        from pipecat.pipeline.task import PipelineTask
        from pipecat.processors.frame_processor import FrameProcessor

        from jarvis.remote_audio import EarInputTransport

        heard: list[bytes] = []

        class Sink(FrameProcessor):
            async def process_frame(self, frame, direction):
                await super().process_frame(frame, direction)
                if isinstance(frame, InputAudioRawFrame):
                    heard.append(frame.audio)
                await self.push_frame(frame, direction)

        ear = Ear(local=False)  # no local engine needed: the frames come from the relay
        params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=SAMPLE_RATE)
        pipeline = Pipeline([EarInputTransport(ear, None, params), Sink()])
        task = PipelineTask(pipeline)
        run = asyncio.create_task(PipelineRunner().run(task))
        try:
            await asyncio.sleep(0.2)  # the StartFrame, and the working queue the input opens
            pcm = frame(0.2)
            self.assertTrue(ear.offer(RELAY, pcm))
            await asyncio.sleep(0.2)
        finally:
            await task.queue_frame(EndFrame())
            await asyncio.wait_for(run, 5)
        self.assertEqual(heard, [pcm])


class TestRelayServer(unittest.IsolatedAsyncioTestCase):
    TOKEN = "test-token"

    async def asyncSetUp(self) -> None:
        self.ear = Ear(local=False)
        self.hub = RelayHub(self.TOKEN, 0)
        # In Jarvis the transport wires this; here the test does, so the client still hears where the ear is.
        self.ear.on_switch = lambda old, new: asyncio.get_running_loop().create_task(
            self.hub.send_ear(new == RELAY)
        )
        # Port 0: the OS hands out a free one, so the suite can run beside a real Jarvis.
        self.server = RelayServer(self.ear, self.hub, port=0)
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    def url(self, path: str) -> str:
        return f"ws://127.0.0.1:{self.server.port}{path}"

    async def test_the_client_script_comes_with_this_servers_url_and_token(self) -> None:
        # The other Mac fetches it with curl, so this is a plain HTTP GET, not a WebSocket.
        script = await asyncio.to_thread(self._fetch, f"/client.py?token={self.TOKEN}")
        url = f"ws://{self.server.public_host()}:{self.server.port}/ws?token={self.TOKEN}"
        self.assertIn(f'DEFAULT_URL = "{url}"', script)
        self.assertNotIn(remote_audio.CLIENT_URL_PLACEHOLDER, script)
        for dependency in ("numpy", "soxr", "websockets", "pyobjc-framework-AVFoundation"):
            self.assertIn(dependency, script)
        # That Mac gets no Jarvis and no Pipecat: the script stands alone.
        for heavy in ("import jarvis", "import pipecat", "mlx", "torch"):
            self.assertNotIn(heavy, script)

    def _fetch(self, path: str) -> str:
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{self.server.port}{path}", timeout=5) as response:
            self.assertEqual(response.headers["Content-Type"], "text/x-python; charset=utf-8")
            return response.read().decode()

    async def test_a_client_speaks_and_is_heard(self) -> None:
        async with connect(self.url(f"/ws?token={self.TOKEN}")) as ws:
            self.assertEqual(json.loads(await ws.recv()), {"type": "hello", "rate": SAMPLE_RATE})
            self.assertEqual(json.loads(await ws.recv()), {"type": "ear", "active": True})
            pcm = frame(0.2)
            await ws.send(pcm)
            self.assertEqual(await asyncio.wait_for(self.ear.frames.get(), 1), pcm)
            await self.hub.send_audio(pcm)
            self.assertEqual(await asyncio.wait_for(ws.recv(), 1), pcm)
        await asyncio.sleep(0.05)
        self.assertIsNone(self.hub.client)
        self.assertFalse(self.ear._online[RELAY])  # gone: this ear has nothing left (the fallback is in TestEar)

    async def test_a_second_client_is_refused_rather_than_sharing_the_ear(self) -> None:
        async with connect(self.url(f"/ws?token={self.TOKEN}")) as first:
            await first.recv()
            with self.assertRaises(Exception) as refused:
                async with connect(self.url(f"/ws?token={self.TOKEN}")) as second:
                    await second.recv()
            self.assertIn("1013", str(refused.exception))

    async def test_a_wrong_or_missing_token_is_refused(self) -> None:
        for path in ("/ws", "/ws?token=nope", "/client.py", "/client.py?token=nope"):
            with self.assertRaises(InvalidStatus) as refused:
                async with connect(self.url(path)):
                    pass
            self.assertEqual(refused.exception.response.status_code, 401)
        self.assertIsNone(self.hub.client)

    async def test_only_the_two_paths_are_served(self) -> None:
        with self.assertRaises(InvalidStatus) as refused:
            async with connect(self.url(f"/?token={self.TOKEN}")):
                pass
        self.assertEqual(refused.exception.response.status_code, 404)

    async def test_a_connected_client_with_a_silent_mic_is_named_in_the_log(self) -> None:
        with mock.patch.object(remote_audio, "SILENT_CLIENT_SECS", 0.01), \
                mock.patch.object(remote_audio, "logger") as log:
            async with connect(self.url(f"/ws?token={self.TOKEN}")):
                await asyncio.sleep(0.1)  # connected, never sends a byte
        self.assertTrue(any("allow Microphone" in str(call) for call in log.error.call_args_list))


class TestClientScript(unittest.IsolatedAsyncioTestCase):
    """The script the other Mac runs: no Jarvis, no Pipecat, and it must agree with the server's wire format."""

    SCRIPT = Path(__file__).resolve().parents[1] / "src" / "jarvis" / "defaults" / "remote" / "client.py"

    def _load(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("jarvis_relay_client", self.SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module

    async def test_it_plays_speech_flushes_and_says_where_the_ear_is(self) -> None:
        client = self._load()
        spoken = np.array([5, -5], dtype=np.int16).tobytes()

        class FakeSocket:
            def __init__(self) -> None:
                self.messages = [
                    json.dumps({"type": "hello", "rate": SAMPLE_RATE}),
                    json.dumps({"type": "ear", "active": True}),
                    spoken,
                    json.dumps({"type": "flush"}),
                ]

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.messages:
                    raise StopAsyncIteration
                return self.messages.pop(0)

            async def send(self, message) -> None:
                raise AssertionError("nothing to send: the mic queue is empty")

        class FakeAudio:
            def __init__(self) -> None:
                self.played: list[bytes] = []
                self.flushes = 0

            def play(self, pcm: bytes) -> None:
                self.played.append(pcm)

            def flush(self) -> None:
                self.flushes += 1

        audio = FakeAudio()
        outbox: asyncio.Queue[bytes] = asyncio.Queue(maxsize=client.BLOCKS)
        with mock.patch("builtins.print") as printed:
            await client.listen(FakeSocket(), audio, outbox)
        self.assertEqual(audio.played, [spoken])
        self.assertEqual(audio.flushes, 1)
        self.assertTrue(any("ear is on this Mac" in str(call) for call in printed.call_args_list))

    def test_it_runs_alone_at_the_servers_rate(self) -> None:
        client = self._load()
        self.assertEqual(client.SAMPLE_RATE, SAMPLE_RATE)  # the server's rate, or audio plays at the wrong speed
        source = self.SCRIPT.read_text()
        self.assertIn("uv run --script", source.splitlines()[0])
        self.assertNotIn("jarvis", "".join(
            line for line in source.splitlines() if line.startswith(("import ", "from "))
        ))


class TestToken(unittest.TestCase):
    def test_the_token_is_made_once_and_kept_private(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "remote_token"
            with mock.patch.object(remote_audio, "TOKEN_PATH", path), \
                    mock.patch.object(remote_audio.settings, "remote_token", ""):
                first = relay_token()
                second = relay_token()
            self.assertEqual(first, second)
            self.assertGreater(len(first), 20)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_a_configured_token_wins_and_writes_nothing(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "remote_token"
            with mock.patch.object(remote_audio, "TOKEN_PATH", path), \
                    mock.patch.object(remote_audio.settings, "remote_token", "pinned"):
                self.assertEqual(relay_token(), "pinned")
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
