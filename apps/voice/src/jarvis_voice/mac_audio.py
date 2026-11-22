"""
Mic and speaker through macOS voice processing, the echo canceller FaceTime uses, so Jarvis hears the user over
its own voice on speakers. Apple cancels only what plays through the same AVAudioEngine, so one engine owns both.
The engine itself is in `voice_engine.py`: `jarvis enroll` and the thin relay client load that one alone.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import threading
from typing import Any

import numpy as np
from loguru import logger
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, StartFrame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameProcessor, FrameProcessorSetup
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

from jarvis_voice.meet_voice import MeetVoice
from jarvis_voice.voice_engine import PLAY_AHEAD_SECS, MicConverter, VoiceAudio

SILENT_MIC_SECS = 3.0  # this long after start without real sound means the mic is not reaching Jarvis
SILENT_MIC_WARNING = (
    "Mic is silent: macOS voice processing delivers no sound. Set System Settings > Sound > Output to speakers "
    "or headphones; a Multi-Output or aggregate device stops the mic."
)
# Logged when Jarvis can't hear the call: the system audio tap needs macOS 14.2 or later and this permission.
TAP_FAILED = (
    "Can't hear the call: allow your terminal (or T3 Code) in System Settings > Privacy & Security > "
    "Screen & System Audio Recording > System Audio Recording Only, then restart Jarvis"
)


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32), ("mElement", ctypes.c_uint32)]


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


class MixBuffer:
    """Thread-safe 16-bit mono PCM ring buffer that mixes system audio (Google Meet) into the steady mic stream."""

    def __init__(self, max_bytes: int = 16000) -> None:
        self._max_bytes = max_bytes & ~1  # ~0.5 s at 16 kHz mono int16
        self._buf = bytearray()
        self._lock = threading.Lock()

    def push(self, pcm16: bytes) -> None:
        if not pcm16:
            return
        with self._lock:
            self._buf.extend(pcm16)
            if len(self._buf) > self._max_bytes:
                del self._buf[: len(self._buf) - self._max_bytes]

    def mix(self, mic_pcm16: bytes) -> bytes:
        if not mic_pcm16:
            return mic_pcm16
        with self._lock:
            if not self._buf:
                return mic_pcm16
            n = min(len(mic_pcm16), len(self._buf)) & ~1
            if n == 0:
                return mic_pcm16
            tap_bytes = bytes(self._buf[:n])
            del self._buf[:n]
        mic = np.frombuffer(mic_pcm16, dtype=np.int16).astype(np.int32).copy()
        tap = np.frombuffer(tap_bytes, dtype=np.int16).astype(np.int32)
        mic[: len(tap)] = np.clip(mic[: len(tap)] + tap, -32768, 32767)
        return mic.astype(np.int16).tobytes()


class MeetingAudioBridge:
    """
    Meeting mode hearing: taps system audio (Google Meet, Zoom, browser) via CoreAudio CATapDescription excluding
    Jarvis's own PID, so Jarvis hears remote participants even on headphones without changing macOS System Output.
    Speaking into the call is `meet_voice.MeetVoice`.
    """

    def __init__(self, out_rate: int = 16000) -> None:
        import AVFoundation
        import objc

        self._av = AVFoundation
        self._objc = objc
        self.mix_buffer = MixBuffer(max_bytes=out_rate)  # 0.5 s of 16-bit mono
        self._tap_converter = MicConverter(out_rate)
        self._ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        self._ca.AudioHardwareCreateProcessTap.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self._ca.AudioHardwareCreateProcessTap.restype = ctypes.c_int32
        self._ca.AudioHardwareDestroyProcessTap.argtypes = [ctypes.c_uint32]
        self._ca.AudioHardwareDestroyProcessTap.restype = ctypes.c_int32
        self._ca.AudioHardwareCreateAggregateDevice.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self._ca.AudioHardwareCreateAggregateDevice.restype = ctypes.c_int32
        self._ca.AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]
        self._ca.AudioHardwareDestroyAggregateDevice.restype = ctypes.c_int32

        self._tap_id = 0
        self._agg_id = 0
        self._tap_engine: Any = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        try:
            pid = ctypes.c_int32(os.getpid())
            own_obj = ctypes.c_uint32(0)
            sz = ctypes.c_uint32(4)
            addr = _AudioObjectPropertyAddress(_fourcc("id2p"), _fourcc("glob"), 0)
            self._ca.AudioObjectGetPropertyData(
                1, ctypes.byref(addr), 4, ctypes.byref(pid), ctypes.byref(sz), ctypes.byref(own_obj)
            )

            CATapDescription = self._objc.lookUpClass("CATapDescription")
            NSDictionary = self._objc.lookUpClass("NSDictionary")
            desc = CATapDescription.alloc().initStereoGlobalTapButExcludeProcesses_([own_obj.value] if own_obj.value else [])
            desc.setName_("JarvisMeetingTap")
            desc.setPrivate_(True)
            tap_uuid = str(desc.UUID().UUIDString())

            tap_id = ctypes.c_uint32(0)
            status = self._ca.AudioHardwareCreateProcessTap(self._objc.pyobjc_id(desc), ctypes.byref(tap_id))
            if status != 0:
                logger.warning(f"{TAP_FAILED} (AudioHardwareCreateProcessTap status {status})")
                return
            self._tap_id = tap_id.value

            agg_dict = NSDictionary.dictionaryWithDictionary_(
                {
                    "name": "JarvisSystemAudioTap",
                    "uid": f"com.jarvis.tap.{os.getpid()}",
                    "private": True,
                    "tapautostart": True,
                    "taps": [{"uid": tap_uuid, "drift": True}],
                }
            )
            agg_id = ctypes.c_uint32(0)
            status = self._ca.AudioHardwareCreateAggregateDevice(self._objc.pyobjc_id(agg_dict), ctypes.byref(agg_id))
            if status != 0:
                logger.warning(f"{TAP_FAILED} (AudioHardwareCreateAggregateDevice status {status})")
                self.stop()
                return
            self._agg_id = agg_id.value

            eng = self._av.AVAudioEngine.alloc().init()
            inp = eng.inputNode()
            inp.AUAudioUnit().setDeviceID_error_(self._agg_id, None)
            fmt = inp.outputFormatForBus_(0)
            rate = int(fmt.sampleRate())
            if rate <= 0:
                logger.warning(f"{TAP_FAILED} (tap sample rate {rate})")
                self.stop()
                return

            def on_tap(buffer, when) -> None:
                n = buffer.frameLength()
                if n > 0:
                    samples = np.frombuffer(buffer.floatChannelData()[0].as_buffer(n), dtype=np.float32).copy()
                    self.mix_buffer.push(self._tap_converter.convert(samples, rate))

            inp.installTapOnBus_bufferSize_format_block_(0, max(1, rate // 50), fmt, on_tap)
            self._tap_engine = eng
            ok, err = eng.startAndReturnError_(None)
            if ok:
                logger.info("Meeting system audio tap active (hearing Google Meet / room audio).")
            else:
                logger.warning(f"Could not start system audio tap: {err}")
                self.stop()
        except Exception as exc:
            logger.warning(f"System audio tap unavailable ({exc!r}); hearing microphone only.")
            self.stop()
        finally:
            # Mark attempted even on failure so dual input/output transport starts do not retry and leak taps.
            self._started = True

    def stop(self) -> None:
        if self._tap_engine is not None:
            self._tap_engine.inputNode().removeTapOnBus_(0)
            self._tap_engine.stop()
            self._tap_engine = None
        if self._agg_id:
            self._ca.AudioHardwareDestroyAggregateDevice(self._agg_id)
            self._agg_id = 0
        if self._tap_id:
            self._ca.AudioHardwareDestroyProcessTap(self._tap_id)
            self._tap_id = 0
        self._started = False


# The engine lives in `voice_engine.py`, without Pipecat; the name stays here for the transports, tests and
# `jarvis enroll` that have always imported it from this module.
MacVoiceAudio = VoiceAudio


class MacVoiceInputTransport(BaseInputTransport):
    def __init__(self, audio: MacVoiceAudio, params: TransportParams) -> None:
        super().__init__(params)
        self._audio = audio
        self._mic: MicConverter | None = None
        self._silence_check: asyncio.Task | None = None

    # Same signature as Pipecat's FrameProcessor.setup, which narrows BaseObject.setup the same way.
    async def setup(self, setup: FrameProcessorSetup) -> None:  # type: ignore[override]
        await super().setup(setup)
        self._mic = MicConverter(self.sample_rate)
        self._audio.on_mic(self._on_mic)

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        self._audio.start()
        self._silence_check = self.create_task(self._warn_if_silent())
        await self.set_transport_ready(frame)

    async def cleanup(self) -> None:
        await super().cleanup()
        if self._silence_check:
            await self.cancel_task(self._silence_check)
        self._audio.stop()

    async def _warn_if_silent(self) -> None:
        # The tap gets no blocks at all when voice processing fails, so this is a timer, not a check on each block.
        await asyncio.sleep(SILENT_MIC_SECS)
        if self._mic is not None and not self._mic.heard:
            logger.error(SILENT_MIC_WARNING)

    def _on_mic(self, samples: np.ndarray, rate: int) -> None:  # audio thread
        mic = self._mic  # set in setup, before the tap is installed
        if mic is None:
            return
        audio = mic.convert(samples, rate)
        if self._audio.bridge is not None:
            audio = self._audio.bridge.mix_buffer.mix(audio)
        if audio:
            frame = InputAudioRawFrame(audio=audio, sample_rate=mic.out_rate, num_channels=1)
            asyncio.run_coroutine_threadsafe(self.push_audio_frame(frame), self.get_event_loop())


class MacVoiceOutputTransport(BaseOutputTransport):
    def __init__(self, audio: MacVoiceAudio, params: TransportParams) -> None:
        super().__init__(params)
        self._audio = audio
        self._until = 0.0  # loop time when everything queued so far has played

    async def setup(self, setup: FrameProcessorSetup) -> None:  # type: ignore[override]  # see the input side
        await super().setup(setup)
        self._audio.connect_player(self.sample_rate)

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        self._audio.start()
        if self._audio.meet_voice is not None:
            await self._audio.meet_voice.start()
        await self.set_transport_ready(frame)

    async def cleanup(self) -> None:
        await super().cleanup()
        if self._audio.meet_voice is not None:
            await self._audio.meet_voice.stop()

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        # The player queues instantly; waiting here paces the output like a blocking speaker write, so an
        # interruption drops the audio Pipecat still holds instead of letting everything already queued play out.
        now = asyncio.get_running_loop().time()
        self._until = max(self._until, now) + self._audio.play(frame.audio, speech=isinstance(frame, TTSAudioRawFrame))
        await asyncio.sleep(max(0.0, self._until - now - PLAY_AHEAD_SECS))
        return True


class MacVoiceTransport(BaseTransport):
    """Local mic and speaker, like Pipecat's LocalAudioTransport, but with Apple's echo canceller in the path."""

    def __init__(self, params: TransportParams, *, meeting: bool = False) -> None:
        super().__init__()
        self._params = params
        self._audio = MacVoiceAudio(
            bridge=MeetingAudioBridge() if meeting else None,  # hears the call
            meet_voice=MeetVoice() if meeting else None,  # speaks into it
        )
        self._input: MacVoiceInputTransport | None = None
        self._output: MacVoiceOutputTransport | None = None
        logger.info("Mic and speaker run through macOS voice processing (echo cancellation).")

    def input(self) -> FrameProcessor:
        if not self._input:
            self._input = MacVoiceInputTransport(self._audio, self._params)
        return self._input
    def output(self) -> FrameProcessor:
        if not self._output:
            self._output = MacVoiceOutputTransport(self._audio, self._params)
        return self._output

    async def set_meeting(self, on: bool) -> None:
        """
        "Meeting mode on" / "meeting mode off" by voice, without rebuilding the transport.

        On: start hearing the call (system audio tap) and speaking into it (the Meet extension's socket). Off: stop
        both. Each half is best effort — a denied Screen & System Audio permission logs a warning and the rest of
        meeting mode still works.
        """
        audio = self._audio
        if on:
            if audio.bridge is None:
                audio.bridge = MeetingAudioBridge()
                audio.bridge.start()
            if audio.meet_voice is None:
                audio.meet_voice = MeetVoice()
                audio.meet_voice.rate = getattr(self._output, "sample_rate", 0) or 16000
                await audio.meet_voice.start()
            return
        if audio.bridge is not None:
            audio.bridge.stop()
        if audio.meet_voice is not None:
            await audio.meet_voice.stop()
