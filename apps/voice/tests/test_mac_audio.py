import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
from pipecat.frames.frames import OutputAudioRawFrame, TTSAudioRawFrame
from pipecat.transports.base_transport import TransportParams

from jarvis_voice.mac_audio import MacVoiceAudio, MacVoiceOutputTransport, MeetingAudioBridge, MicConverter, MixBuffer
from jarvis_voice.remote_audio import RELAY


class TestMicConverter(unittest.TestCase):
    """The processed mic (float32 at 48 kHz) reaches Jarvis as 16-bit PCM at 16 kHz."""

    def test_48k_float_becomes_16k_pcm(self) -> None:
        mic = MicConverter(16000)
        tone = np.sin(np.arange(4800) * 0.05).astype(np.float32) * 0.5  # 100 ms at 48 kHz
        out = b"".join(mic.convert(tone[i : i + 960], 48000) for i in range(0, 4800, 960))  # 20 ms blocks
        self.assertAlmostEqual(len(out) / 2, 1600, delta=200)  # 100 ms at 16 kHz; the stream holds back a little
        self.assertLess(np.abs(np.frombuffer(out, dtype=np.int16)).max(), 32767 * 0.6)  # same level, not clipped

    def test_same_rate_only_changes_the_sample_format(self) -> None:
        out = MicConverter(16000).convert(np.array([0.0, 0.5, -1.0, 2.0], dtype=np.float32), 16000)
        self.assertEqual(np.frombuffer(out, dtype=np.int16).tolist(), [0, 16383, -32767, 32767])  # 2.0 is clipped


class TestMicHeard(unittest.TestCase):
    """A dead voice-processing mic gives nothing or near zeros; room noise counts as a working mic."""

    def test_near_zeros_are_not_heard(self) -> None:
        mic = MicConverter(16000)
        for _ in range(150):  # 3 s of 20 ms blocks at 48 kHz
            mic.convert(np.full(960, 3e-5, dtype=np.float32), 48000)
        self.assertFalse(mic.heard)

    def test_one_block_of_room_noise_is_heard_for_good(self) -> None:
        mic = MicConverter(16000)
        mic.convert(np.full(960, 5e-3, dtype=np.float32), 48000)
        mic.convert(np.zeros(960, dtype=np.float32), 48000)
        self.assertTrue(mic.heard)


class TestMixBuffer(unittest.TestCase):
    """System audio (Google Meet) is mixed into the steady mic stream without clipping or overflowing."""

    def test_empty_buffer_leaves_mic_unchanged(self) -> None:
        buf = MixBuffer()
        mic = np.array([100, -200, 300], dtype=np.int16).tobytes()
        self.assertEqual(buf.mix(mic), mic)

    def test_sums_and_clips_system_audio_into_mic(self) -> None:
        buf = MixBuffer()
        buf.push(np.array([1000, 30000, -30000], dtype=np.int16).tobytes())
        mic = np.array([500, 10000, -10000, 42], dtype=np.int16).tobytes()
        mixed = np.frombuffer(buf.mix(mic), dtype=np.int16).tolist()
        self.assertEqual(mixed, [1500, 32767, -32768, 42])

    def test_caps_buffered_audio_to_max_bytes(self) -> None:
        buf = MixBuffer(max_bytes=4)
        buf.push(np.array([1, 2, 3, 4], dtype=np.int16).tobytes())
        mic = np.zeros(2, dtype=np.int16).tobytes()
        self.assertEqual(np.frombuffer(buf.mix(mic), dtype=np.int16).tolist(), [3, 4])


def _fake_audio() -> MacVoiceAudio:
    audio = object.__new__(MacVoiceAudio)
    audio._av = MagicMock()
    pcm_buf = MagicMock()
    pcm_buf.floatChannelData.return_value = [MagicMock(as_buffer=lambda n: bytearray(n * 4))]
    audio._av.AVAudioPCMBuffer.alloc.return_value.initWithPCMFormat_frameCapacity_.return_value = pcm_buf
    audio.player = MagicMock()
    audio._play_format = MagicMock()
    audio._play_format.sampleRate.return_value = 16000.0
    audio.bridge = None
    audio.meet_voice = MagicMock()
    return audio


class TestMacVoiceOutput(unittest.IsolatedAsyncioTestCase):
    """TTS speech is sent to Meet, while notification/mute beeps play only on local speakers."""

    def test_play_skips_meet_voice_when_not_speech(self) -> None:
        audio = _fake_audio()
        pcm = np.array([100, -100], dtype=np.int16).tobytes()

        audio.play(pcm, speech=False)
        audio.meet_voice.send.assert_not_called()
        audio.player.scheduleBuffer_completionHandler_.assert_called_once()

        audio.play(pcm)
        audio.meet_voice.send.assert_called_once_with(pcm)

    async def test_write_audio_frame_marks_only_tts_frames_as_speech(self) -> None:
        audio = _fake_audio()
        transport = MacVoiceOutputTransport(audio, TransportParams(audio_out_enabled=True))
        tts_pcm = np.array([120, -120], dtype=np.int16).tobytes()
        beep_pcm = np.array([300, -300], dtype=np.int16).tobytes()

        with patch.object(audio, "play", wraps=audio.play) as play_spy:
            await transport.write_audio_frame(TTSAudioRawFrame(audio=tts_pcm, sample_rate=16000, num_channels=1))
            await transport.write_audio_frame(OutputAudioRawFrame(audio=beep_pcm, sample_rate=16000, num_channels=1))

        self.assertEqual(play_spy.call_args_list[0].kwargs, {"speech": True})
        self.assertEqual(play_spy.call_args_list[1].kwargs, {"speech": False})
        audio.meet_voice.send.assert_called_once_with(tts_pcm)


class TestVoiceAudio(unittest.TestCase):
    """The shared engine: a flush drops queued speech, and does nothing before a player exists."""

    def test_flush_drops_queued_audio_and_keeps_playing(self) -> None:
        audio = _fake_audio()
        audio.flush()
        audio.player.stop.assert_called_once()
        audio.player.play.assert_called_once()

    def test_flush_before_the_player_is_connected_does_nothing(self) -> None:
        audio = _fake_audio()
        audio._play_format = None
        audio.flush()
        audio.player.stop.assert_not_called()


class TestMeetingAudioBridge(unittest.TestCase):
    """Failed system-audio tap setup cleans up CoreAudio handles and is not retried on dual start()."""

    def _make_bridge(self, *, sample_rate: float = 48000.0, start_ok: bool = True, start_exc: Exception | None = None):
        fake_ca = MagicMock()

        def create_tap(_desc, tap_ptr) -> int:
            tap_ptr._obj.value = 101
            return 0

        def create_agg(_dict, agg_ptr) -> int:
            agg_ptr._obj.value = 202
            return 0

        fake_ca.AudioHardwareCreateProcessTap.side_effect = create_tap
        fake_ca.AudioHardwareCreateAggregateDevice.side_effect = create_agg

        fake_eng = MagicMock()
        fake_inp = fake_eng.inputNode.return_value
        fake_inp.outputFormatForBus_.return_value.sampleRate.return_value = sample_rate
        if start_exc is not None:
            fake_eng.startAndReturnError_.side_effect = start_exc
        else:
            fake_eng.startAndReturnError_.return_value = (start_ok, None if start_ok else "tap failed")

        fake_av = MagicMock()
        fake_av.AVAudioEngine.alloc.return_value.init.return_value = fake_eng
        fake_objc = MagicMock()

        with (
            patch.dict(sys.modules, {"AVFoundation": fake_av, "objc": fake_objc}),
            patch("jarvis_voice.mac_audio.ctypes.CDLL", return_value=fake_ca),
        ):
            bridge = MeetingAudioBridge()
        return bridge, fake_ca, fake_inp

    def test_start_engine_failure_cleans_up_and_does_not_retry(self) -> None:
        bridge, fake_ca, _ = self._make_bridge(start_ok=False)
        with patch.object(bridge, "stop", wraps=bridge.stop) as stop_spy:
            bridge.start()
            bridge.start()

        stop_spy.assert_called_once()
        self.assertEqual(bridge._tap_id, 0)
        self.assertEqual(bridge._agg_id, 0)
        self.assertIsNone(bridge._tap_engine)
        fake_ca.AudioHardwareDestroyAggregateDevice.assert_called_once_with(202)
        fake_ca.AudioHardwareDestroyProcessTap.assert_called_once_with(101)
        self.assertEqual(fake_ca.AudioHardwareCreateProcessTap.call_count, 1)

    def test_zero_sample_rate_cleans_up_and_does_not_retry(self) -> None:
        bridge, fake_ca, fake_inp = self._make_bridge(sample_rate=0.0)
        with patch.object(bridge, "stop", wraps=bridge.stop) as stop_spy:
            bridge.start()
            bridge.start()

        stop_spy.assert_called_once()
        fake_inp.installTapOnBus_bufferSize_format_block_.assert_not_called()
        self.assertEqual(bridge._tap_id, 0)
        self.assertEqual(bridge._agg_id, 0)
        fake_ca.AudioHardwareDestroyAggregateDevice.assert_called_once_with(202)
        fake_ca.AudioHardwareDestroyProcessTap.assert_called_once_with(101)
        self.assertEqual(fake_ca.AudioHardwareCreateProcessTap.call_count, 1)

    def test_exception_in_start_cleans_up_and_does_not_retry(self) -> None:
        bridge, fake_ca, _ = self._make_bridge(start_exc=RuntimeError("boom"))
        with patch.object(bridge, "stop", wraps=bridge.stop) as stop_spy:
            bridge.start()
            bridge.start()

        stop_spy.assert_called_once()
        self.assertEqual(bridge._tap_id, 0)
        self.assertEqual(bridge._agg_id, 0)
        fake_ca.AudioHardwareDestroyAggregateDevice.assert_called_once_with(202)
        fake_ca.AudioHardwareDestroyProcessTap.assert_called_once_with(101)
        self.assertEqual(fake_ca.AudioHardwareCreateProcessTap.call_count, 1)


class TestHybridWithAPairedMac(unittest.IsolatedAsyncioTestCase):
    """A paired Mac owns the mic and speaker while connected; this Mac's take them back when it goes."""

    def setUp(self) -> None:
        from jarvis_voice.remote_audio import Ear, RelayHub

        self.hub = RelayHub()
        self.ear = Ear(local=True)
        self.audio = _fake_audio()
        self.params = TransportParams(audio_in_enabled=True, audio_out_enabled=True)

    def _client(self):
        class Client:
            async def send_bytes(self, data: bytes) -> None: ...
            async def send_text(self, data: str) -> None: ...

        return Client()

    async def test_local_mic_is_ignored_while_a_paired_mac_is_connected(self) -> None:
        from jarvis_voice.mac_audio import MacVoiceInputTransport

        transport = MacVoiceInputTransport(self.audio, self.params, hub=self.hub, ear=self.ear)
        transport._mic = MicConverter(16000)
        pushed = []

        async def push(frame) -> None:
            pushed.append(frame)

        transport.push_audio_frame = push  # type: ignore[method-assign]
        transport.get_event_loop = lambda: asyncio.get_running_loop()  # type: ignore[method-assign]
        loud = np.full(960, 0.5, dtype=np.float32)

        def feed() -> None:
            for _ in range(5):  # the resampler holds the first block back, so one block is not enough
                transport._on_mic(loud, 48000)

        feed()
        await asyncio.sleep(0.05)
        self.assertGreater(len(pushed), 0)  # no client: this Mac's mic is the ear
        local = len(pushed)

        self.hub.attach(self._client())
        self.ear.set_online(RELAY, True)
        feed()
        await asyncio.sleep(0.05)
        self.assertEqual(len(pushed), local)  # the paired Mac is the ear now

        self.hub.detach(self.hub.client)
        self.ear.set_online(RELAY, False)
        feed()
        await asyncio.sleep(0.05)
        self.assertGreater(len(pushed), local)

    async def test_the_paired_macs_mic_reaches_the_pipeline(self) -> None:
        from jarvis_voice.mac_audio import MacVoiceInputTransport

        transport = MacVoiceInputTransport(self.audio, self.params, hub=self.hub, ear=self.ear)
        transport._sample_rate = 16000
        pushed = []

        async def push(frame) -> None:
            pushed.append(frame)

        transport.push_audio_frame = push  # type: ignore[method-assign]
        pump = asyncio.create_task(transport._pump_remote())
        try:
            self.hub.attach(self._client())
            self.ear.set_online(RELAY, True)
            self.hub.push_mic(bytes([1, 0, 2, 0]))
            await asyncio.sleep(0.02)
        finally:
            pump.cancel()
        self.assertEqual([f.audio for f in pushed], [bytes([1, 0, 2, 0])])
        self.assertEqual(pushed[0].sample_rate, 16000)

    async def test_speech_goes_to_the_paired_mac_and_back_to_this_one(self) -> None:
        from jarvis_voice.mac_audio import MacVoiceOutputTransport

        transport = MacVoiceOutputTransport(self.audio, self.params, hub=self.hub, ear=self.ear)
        self.audio.play = MagicMock(return_value=0.1)  # type: ignore[method-assign]
        pcm = np.array([100, -100], dtype=np.int16).tobytes()

        with patch.object(self.hub, "send_audio", new=AsyncMock(return_value=0.1)) as send, \
                patch.object(asyncio, "sleep", new=AsyncMock()):
            await transport.write_audio_frame(OutputAudioRawFrame(audio=pcm, sample_rate=24000, num_channels=1))
            self.audio.play.assert_called_once()  # no paired Mac: this Mac's speaker
            send.assert_not_awaited()

            self.hub.attach(self._client())
            self.ear.set_online(RELAY, True)
            await transport.write_audio_frame(OutputAudioRawFrame(audio=pcm, sample_rate=24000, num_channels=1))
            send.assert_awaited_once_with(pcm, sample_rate=24000)  # the paired Mac's speaker
            self.audio.play.assert_called_once()

            self.hub.detach(self.hub.client)
            self.ear.set_online(RELAY, False)
            await transport.write_audio_frame(OutputAudioRawFrame(audio=pcm, sample_rate=24000, num_channels=1))
        self.assertEqual(self.audio.play.call_count, 2)

    async def test_an_interruption_flushes_both_the_paired_mac_and_this_one(self) -> None:
        from jarvis_voice.mac_audio import MacVoiceOutputTransport

        transport = MacVoiceOutputTransport(self.audio, self.params, hub=self.hub)
        self.hub.attach(self._client())
        transport._until = 99.0
        with patch.object(self.hub, "flush", new=AsyncMock()) as flush:
            await transport._interrupt()
        flush.assert_awaited_once()
        self.audio.player.stop.assert_called_once()  # this Mac's queued speech goes too
        self.assertEqual(transport._until, 0.0)

    async def test_a_mac_with_no_local_audio_still_paces_the_pipeline(self) -> None:
        from jarvis_voice.mac_audio import MacVoiceOutputTransport

        transport = MacVoiceOutputTransport(None, self.params, hub=self.hub)
        pcm = bytes(3200)  # 0.1 s at 16 kHz
        with patch.object(asyncio, "sleep", new=AsyncMock()) as sleep:
            self.assertTrue(
                await transport.write_audio_frame(OutputAudioRawFrame(audio=pcm, sample_rate=16000, num_channels=1))
            )
        sleep.assert_awaited_once()  # timed, so no frame is dropped at once
        self.assertGreater(transport._until, 0.0)

    def test_no_local_mic_means_remote_only_and_meeting_says_so(self) -> None:
        from jarvis_voice import mac_audio

        with patch.object(mac_audio, "MacVoiceAudio", side_effect=RuntimeError("no input")), \
                patch.object(mac_audio.logger, "warning") as warning:
            transport = mac_audio.MacVoiceTransport(self.params, hub=self.hub)

        self.assertFalse(transport.has_local_audio)
        self.assertEqual(self.hub.on_attach, [transport._remote_attached])  # still handed over to a paired Mac
        self.assertIsNotNone(transport.input())  # still built, so the paired Mac's mic has somewhere to land
        warning.assert_called_once()
        self.assertIn("No local mic", str(warning.call_args[0][0]))

    def test_a_mac_with_a_speaker_but_no_mic_is_remote_only(self) -> None:
        from jarvis_voice import mac_audio

        speaker_only = MagicMock()
        speaker_only.has_mic_input.return_value = False
        with patch.object(mac_audio, "MacVoiceAudio", return_value=speaker_only), \
                patch.object(mac_audio.logger, "warning"):
            transport = mac_audio.MacVoiceTransport(self.params, hub=self.hub)
        self.assertFalse(transport.has_local_audio)
        speaker_only.stop.assert_called_once()  # the engine it made is released, not left holding the speaker

    async def test_meeting_mode_without_local_audio_warns_instead_of_crashing(self) -> None:
        from jarvis_voice import mac_audio

        with patch.object(mac_audio, "MacVoiceAudio", side_effect=RuntimeError("no input")):
            transport = mac_audio.MacVoiceTransport(self.params, hub=self.hub)
        with patch.object(mac_audio.logger, "warning") as warning:
            await transport.set_meeting(True)
        self.assertIn("Meeting mode needs", str(warning.call_args[0][0]))

    def test_the_local_speaker_hands_over_at_once(self) -> None:
        from jarvis_voice import mac_audio

        with patch.object(mac_audio, "MacVoiceAudio"):
            transport = mac_audio.MacVoiceTransport(self.params, hub=self.hub)
        transport._output = MagicMock()
        transport._audio = MagicMock()
        transport._remote_attached()
        transport._output.handoff.assert_called_once()  # its stale pacing would hold the paired Mac's first word
        transport._audio.flush.assert_called_once()  # and anything this Mac was still saying stops now

    def test_the_handoff_warns_that_the_voiceprint_belongs_to_this_mac(self) -> None:
        from jarvis_voice import mac_audio

        with patch.object(mac_audio, "MacVoiceAudio"):
            transport = mac_audio.MacVoiceTransport(self.params, hub=self.hub)
        transport._audio = MagicMock()
        with patch.object(mac_audio.settings, "master_check", True), \
                patch.object(mac_audio.logger, "warning") as warning:
            transport._remote_attached()
        self.assertTrue(any("SPEAKER_THRESHOLD" in str(call) for call in warning.call_args_list))

    def test_input_meter_reports_the_peak_reaching_jarvis(self) -> None:
        from jarvis_voice import telemetry
        from jarvis_voice.mac_audio import MacVoiceInputTransport

        seen: list[dict] = []
        telemetry.bind(lambda kind, **fields: seen.append({"kind": kind, **fields}))
        try:
            transport = MacVoiceInputTransport(self.audio, self.params, hub=self.hub, ear=self.ear)
            pcm = np.array([0, 8192, -16384], dtype=np.int16).tobytes()
            transport._sample_level(pcm, "local")
        finally:
            telemetry.bind(None)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["kind"], "voice.level")
        self.assertAlmostEqual(seen[0]["peak"], 0.5, places=3)
        self.assertEqual(seen[0]["source"], "local")


if __name__ == "__main__":
    unittest.main()
