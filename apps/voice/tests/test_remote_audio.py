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

from jarvis_voice import remote_audio
from jarvis_voice.remote_audio import (
    SAMPLE_RATE,
    RelayHub,
    RemoteVoiceOutputTransport,
    RemoteVoiceTransport,
    relay_token,
)


class FakeConnection:
    """A relay client's socket, recording what Jarvis sends it."""

    def __init__(self) -> None:
        self.sent: list[bytes | str] = []

    async def send_bytes(self, message: bytes) -> None:
        self.sent.append(message)

    async def send_text(self, message: str) -> None:
        self.sent.append(message)


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

    def test_mic_audio_drops_the_oldest_block_rather_than_falling_behind(self) -> None:
        for n in range(remote_audio.MIC_QUEUE_BLOCKS + 2):
            self.hub.push_mic(bytes([n]))
        self.assertEqual(self.hub.mic_queue.qsize(), remote_audio.MIC_QUEUE_BLOCKS)
        self.assertEqual(self.hub.dropped_mic, 2)
        self.assertEqual(self.hub.mic_queue.get_nowait(), bytes([2]))  # the first two went

    def test_a_new_client_is_not_heard_through_the_last_ones_leftovers(self) -> None:
        first = FakeConnection()
        self.hub.attach(first)
        self.hub.push_mic(b"old")
        self.hub.detach(first)  # what the previous client left is no longer wanted
        self.assertEqual(self.hub.mic_queue.qsize(), 0)

    async def test_audio_with_no_client_is_dropped_but_still_timed(self) -> None:
        pcm = bytes(SAMPLE_RATE // 2)  # 0.25 s of 16-bit mono
        self.assertAlmostEqual(await self.hub.send_audio(pcm), 0.25, places=3)
        await self.hub.flush()  # nothing to flush, and no error

    async def test_audio_and_flush_reach_the_client(self) -> None:
        client = FakeConnection()
        self.hub.attach(client)
        pcm = bytes(SAMPLE_RATE * 2 // 10)  # 0.1 s of 16-bit mono
        self.assertAlmostEqual(await self.hub.send_audio(pcm), 0.1, places=3)
        await self.hub.flush()
        self.assertEqual(client.sent, [pcm, json.dumps({"type": "flush"})])

    async def test_wait_for_client_returns_when_one_connects(self) -> None:
        waiter = asyncio.create_task(self.hub.wait_for_client())
        await asyncio.sleep(0)  # let it start and block
        self.assertFalse(waiter.done())
        self.hub.attach(FakeConnection())
        await asyncio.wait_for(waiter, 1)

    async def test_a_client_that_stops_reading_is_dropped_not_the_transport(self) -> None:
        class Stalled(FakeConnection):
            async def send_bytes(self, message) -> None:
                await asyncio.sleep(60)  # a Wi-Fi link that went quiet with the socket still up

        stalled = Stalled()
        self.hub.attach(stalled)
        with mock.patch.object(remote_audio, "SEND_TIMEOUT_SECS", 0.01):
            await self.hub.send_audio(bytes(320))
        self.assertIsNone(self.hub.client)  # gone, so the next Mac can take the mic


class TestOutputTransport(unittest.IsolatedAsyncioTestCase):
    def _transport(self) -> tuple[RemoteVoiceOutputTransport, FakeConnection]:
        hub = RelayHub()
        client = FakeConnection()
        hub.attach(client)
        params = TransportParams(audio_out_enabled=True, audio_out_sample_rate=SAMPLE_RATE)
        return RemoteVoiceOutputTransport(hub, params), client

    async def test_speech_goes_over_whole_and_paced(self) -> None:
        transport, client = self._transport()
        pcm = bytes(SAMPLE_RATE * 2 // 10)  # 0.1 s
        with mock.patch.object(remote_audio.asyncio, "sleep", new=mock.AsyncMock()) as sleep:
            self.assertTrue(await transport.write_audio_frame(OutputAudioRawFrame(audio=pcm, sample_rate=SAMPLE_RATE,
                                                                                  num_channels=1)))
        self.assertEqual(client.sent, [pcm])
        sleep.assert_awaited_once()  # paced, like a blocking speaker write

    async def test_an_interruption_flushes_what_the_other_mac_queued(self) -> None:
        transport, client = self._transport()
        transport._until = 99.0  # as if a long reply were playing
        await transport._interrupt()
        self.assertEqual(client.sent, [json.dumps({"type": "flush"})])
        self.assertEqual(transport._until, 0.0)  # the next frame is not held back by the old timing


class TestRelayServer(unittest.IsolatedAsyncioTestCase):
    TOKEN = "test-token"

    async def asyncSetUp(self) -> None:
        # Port 0: the OS hands out a free one, so the suite can run beside a real Jarvis.
        self.transport = RemoteVoiceTransport(
            TransportParams(audio_in_enabled=True, audio_out_enabled=True), port=0, token=self.TOKEN
        )
        await self.transport.start_server()
        self.port = self.transport.port

    async def asyncTearDown(self) -> None:
        await self.transport.stop_server()

    def url(self, path: str) -> str:
        return f"ws://127.0.0.1:{self.port}{path}"

    async def test_the_client_script_comes_with_this_servers_url(self) -> None:
        # The laptop fetches it with curl, so this is a plain HTTP GET, not a WebSocket.
        script = await asyncio.to_thread(self._fetch, "/client.py")
        # The address is the one the laptop used, so a LAN IP or a Tailscale name works without mDNS.
        self.assertIn(f'DEFAULT_URL = "ws://127.0.0.1:{self.port}/ws"', script)
        self.assertIn(f'DEFAULT_SERVER = "http://127.0.0.1:{self.port}"', script)
        # No secret is baked into the script: the laptop pairs once and keeps its own.
        self.assertNotIn("token=", script)
        self.assertNotIn(remote_audio.CLIENT_URL_PLACEHOLDER, script)
        self.assertNotIn(remote_audio.CLIENT_SERVER_PLACEHOLDER, script)
        for dependency in ("numpy", "soxr", "websockets", "pyobjc-framework-AVFoundation"):
            self.assertIn(dependency, script)
        # The laptop gets no Jarvis and no Pipecat: the script stands alone.
        for heavy in ("import jarvis", "import pipecat", "mlx", "torch"):
            self.assertNotIn(heavy, script)

    async def test_a_wildcard_fetch_bakes_in_the_advertised_name(self) -> None:
        # The logged command names the Bonjour name; a request that reaches us as `0.0.0.0` must not bake that in.
        self.assertNotIn("0.0.0.0", self.transport.client_script("0.0.0.0"))
        self.assertIn(f"ws://{self.transport.public_host()}:{self.port}/ws", self.transport.client_script("0.0.0.0"))
        self.assertIn(f"ws://192.168.1.20:{self.port}/ws", self.transport.client_script("192.168.1.20"))

    def _fetch(self, path: str) -> str:
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as response:
            self.assertEqual(response.headers["Content-Type"], "text/x-python; charset=utf-8")
            return response.read().decode()

    async def test_a_client_speaks_and_is_heard(self) -> None:
        async with connect(self.url(f"/ws?token={self.TOKEN}")) as ws:
            self.assertEqual(json.loads(await ws.recv()), {"type": "hello", "rate": SAMPLE_RATE})
            pcm = np.array([100, -100], dtype=np.int16).tobytes()
            await ws.send(pcm)
            self.assertEqual(await asyncio.wait_for(self.transport.hub.mic_queue.get(), 1), pcm)
            self.assertGreater(self.transport.hub.received, 0)
            await self.transport.hub.send_audio(pcm)
            self.assertEqual(await asyncio.wait_for(ws.recv(), 1), pcm)
        await asyncio.sleep(0.05)
        self.assertIsNone(self.transport.hub.client)

    async def test_a_connected_client_with_a_silent_mic_is_named_in_the_log(self) -> None:
        with mock.patch.object(remote_audio, "SILENT_CLIENT_SECS", 0.01), \
                mock.patch.object(remote_audio, "logger") as log:
            async with connect(self.url(f"/ws?token={self.TOKEN}")) as ws:
                await ws.recv()
                await asyncio.sleep(0.1)  # connected, never sends a byte
        self.assertTrue(any("allow Microphone" in str(call) for call in log.error.call_args_list))

    async def test_a_second_client_is_refused_rather_than_sharing_the_mic(self) -> None:
        async with connect(self.url(f"/ws?token={self.TOKEN}")) as first:
            await first.recv()
            with self.assertRaises(Exception) as refused:
                async with connect(self.url(f"/ws?token={self.TOKEN}")) as second:
                    await second.recv()
            self.assertIn("1013", str(refused.exception))

    async def test_a_wrong_or_missing_token_is_refused_on_the_socket(self) -> None:
        for path in ("/ws", "/ws?token=nope"):
            with self.assertRaises(InvalidStatus) as refused:
                async with connect(self.url(path)):
                    pass
            self.assertEqual(refused.exception.response.status_code, 401)
        self.assertIsNone(self.transport.hub.client)

    async def test_the_client_script_itself_is_open(self) -> None:
        # Bootstrap: it must arrive before the laptop has a secret, and it holds no secret.
        script = await asyncio.to_thread(self._fetch, "/client.py")
        self.assertIn("DEFAULT_SERVER", script)
        self.assertNotIn("token=", script)

    async def test_only_the_two_paths_are_served(self) -> None:
        with self.assertRaises(InvalidStatus) as refused:
            async with connect(self.url(f"/?token={self.TOKEN}")):
                pass
        self.assertEqual(refused.exception.response.status_code, 404)


class TestPairedDevice(unittest.IsolatedAsyncioTestCase):
    """A paired laptop connects with its own secret; revoking it stops that laptop and nothing else."""

    TOKEN = "shared-token"

    async def asyncSetUp(self) -> None:
        import tempfile

        from jarvis_core.devices import DeviceStore

        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = DeviceStore(self.dir / "devices.json")
        self.transport = RemoteVoiceTransport(
            TransportParams(audio_in_enabled=True, audio_out_enabled=True),
            port=0,
            token=self.TOKEN,
            devices=self.store,
        )
        await self.transport.start_server()
        self.port = self.transport.port

    async def asyncTearDown(self) -> None:
        await self.transport.stop_server()

    def url(self, token: str) -> str:
        return f"ws://127.0.0.1:{self.port}/ws?token={token}"

    async def _hello(self, token: str) -> dict:
        async with connect(self.url(token)) as ws:
            return json.loads(await ws.recv())

    async def test_a_paired_secret_is_accepted_and_a_revoked_one_is_refused(self) -> None:
        device, secret = self.store.add("Test laptop")
        self.assertEqual((await self._hello(secret))["type"], "hello")
        await asyncio.sleep(0.05)  # the hub drops the client after the socket closes

        self.store.revoke(device.id)
        with self.assertRaises(InvalidStatus) as refused:
            await self._hello(secret)
        self.assertEqual(refused.exception.response.status_code, 401)

        # The shared token still works: revocation is per device.
        self.assertEqual((await self._hello(self.TOKEN))["type"], "hello")


class TestClientScript(unittest.IsolatedAsyncioTestCase):
    """The script the other Mac runs: no Jarvis, no Pipecat, and it must agree with the server's wire format."""

    SCRIPT = Path(__file__).resolve().parents[1] / "src" / "jarvis_voice" / "defaults" / "remote" / "client.py"

    def _load(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("jarvis_relay_client", self.SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module

    async def test_it_plays_speech_and_flushes_on_an_interruption(self) -> None:
        client = self._load()
        spoken = np.array([5, -5], dtype=np.int16).tobytes()

        class FakeSocket:
            def __init__(self) -> None:
                self.messages = [json.dumps({"type": "hello", "rate": SAMPLE_RATE}), spoken,
                                 json.dumps({"type": "flush"})]

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
        await client.listen(FakeSocket(), audio, outbox)
        self.assertEqual(audio.played, [spoken])
        self.assertEqual(audio.flushes, 1)

    def test_it_runs_alone_at_the_servers_rate(self) -> None:
        client = self._load()
        self.assertEqual(client.SAMPLE_RATE, SAMPLE_RATE)  # the server's rate, or audio plays at the wrong speed
        source = self.SCRIPT.read_text()
        self.assertIn("uv run --script", source.splitlines()[0])
        self.assertNotIn("jarvis", "".join(
            line for line in source.splitlines() if line.startswith(("import ", "from "))
        ))

    async def test_it_builds_its_mic_resampler_only_once_the_rate_is_known(self) -> None:
        """soxr 1.0 refuses a 0 Hz stream, so a placeholder at startup would crash the client before the mic starts."""
        client = self._load()
        bridge = self._bridge(client)
        self.assertIsNone(bridge._mic)  # the rate is the mic's own, unknown until on_mic installs the tap
        pcm = bridge._to_pcm(np.array([0.5, -0.5], dtype=np.float32))
        self.assertEqual(np.frombuffer(pcm, dtype=np.int16).tolist(), [16383, -16383])

    async def test_it_resamples_a_mic_at_another_rate(self) -> None:
        client = self._load()
        bridge = self._bridge(client)

        class FakeResampler:
            chunks = 0

            def resample_chunk(self, samples):
                self.chunks += 1
                return samples[:1]

        resampler = FakeResampler()
        bridge._mic = resampler  # what on_mic builds for a 48 kHz mic
        bridge._to_pcm(np.zeros(480, dtype=np.float32))
        self.assertEqual(resampler.chunks, 1)

    def _bridge(self, client):
        """An AudioBridge over a stubbed AVFoundation: real pyobjc audio would need a mic and a speaker."""
        engine = mock.MagicMock()
        av = mock.MagicMock()
        av.AVAudioEngine.alloc.return_value.init.return_value = engine
        engine.inputNode.return_value.setVoiceProcessingEnabled_error_.return_value = (True, None)
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        with mock.patch.dict("sys.modules", {"AVFoundation": av}):
            return client.AudioBridge(asyncio.Queue(maxsize=client.BLOCKS), loop)


class TestToken(unittest.TestCase):
    def test_the_relay_uses_the_one_gateway_token(self) -> None:
        with mock.patch.object(remote_audio.settings, "remote_token", ""), \
                mock.patch.object(remote_audio, "brain_token", return_value="gateway-token"):
            self.assertEqual(relay_token(), "gateway-token")

    def test_a_configured_token_wins(self) -> None:
        with mock.patch.object(remote_audio.settings, "remote_token", "pinned"), \
                mock.patch.object(remote_audio, "brain_token", return_value="gateway-token"):
            self.assertEqual(relay_token(), "pinned")


if __name__ == "__main__":
    unittest.main()
