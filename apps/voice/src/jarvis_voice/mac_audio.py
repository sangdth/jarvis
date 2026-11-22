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
from typing import TYPE_CHECKING, Any

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

from jarvis_voice.config import settings
from jarvis_voice.meet_voice import MeetVoice
from jarvis_voice.remote_audio import LOCAL, RELAY, Ear
from jarvis_voice.voice_engine import PLAY_AHEAD_SECS, MicConverter, VoiceAudio

if TYPE_CHECKING:
    from jarvis_voice.remote_audio import RelayHub

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
    """This Mac's mic or a paired Mac's, whichever `Ear` says is live: one stream, one pipeline."""

    def __init__(
        self,
        audio: MacVoiceAudio | None,
        params: TransportParams,
        *,
        hub: "RelayHub | None" = None,
        ear: Ear | None = None,
    ) -> None:
        super().__init__(params)
        self._audio = audio
        self._hub = hub
        self._ear = ear or Ear(local=audio is not None)
        self._mic: MicConverter | None = None
        self._silence_check: asyncio.Task | None = None
        self._remote_pump: asyncio.Task | None = None

    # Same signature as Pipecat's FrameProcessor.setup, which narrows BaseObject.setup the same way.
    async def setup(self, setup: FrameProcessorSetup) -> None:  # type: ignore[override]
        await super().setup(setup)
        if self._audio is not None:
            try:
                self._mic = MicConverter(self.sample_rate)
                self._audio.on_mic(self._on_mic)
            except Exception as exc:
                # Losing the local mic must not take the pipeline down: a paired Mac's mic still works.
                self._mic = None
                logger.error(f"This Mac's mic could not be opened ({exc!r}); a paired Mac's is used when it connects.")

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if self._audio is not None:
            self._audio.start()
            self._silence_check = self.create_task(self._warn_if_silent())
        if self._hub is not None:
            self._remote_pump = self.create_task(self._pump_remote())
        await self.set_transport_ready(frame)

    async def cleanup(self) -> None:
        await super().cleanup()
        for task in (self._silence_check, self._remote_pump):
            if task:
                await self.cancel_task(task)
        self._silence_check = self._remote_pump = None
        if self._audio is not None:
            self._audio.stop()

    @property
    def _remote(self) -> bool:
        return self._ear.active == RELAY

    async def _warn_if_silent(self) -> None:
        # The tap gets no blocks at all when voice processing fails, so this is a timer, not a check on each block.
        await asyncio.sleep(SILENT_MIC_SECS)
        if self._remote:
            return  # the paired Mac's mic is the one in use, and the relay logs a silent client itself
        if self._mic is not None and not self._mic.heard:
            logger.error(SILENT_MIC_WARNING)

    async def _pump_remote(self) -> None:
        """The paired Mac's mic, as this pipeline's input; in a meeting the call's audio is mixed in here too."""
        assert self._hub is not None
        while True:
            pcm = await self._hub.mic_queue.get()
            audio = self._audio
            if audio is not None and audio.bridge is not None:
                pcm = audio.bridge.mix_buffer.mix(pcm)  # the call, on the host where the tap runs
            if pcm and self._ear.accept(RELAY, pcm):
                await self.push_audio_frame(
                    InputAudioRawFrame(audio=pcm, sample_rate=self.sample_rate, num_channels=1)
                )

    def _on_mic(self, samples: np.ndarray, rate: int) -> None:  # audio thread
        mic = self._mic  # set in setup, before the tap is installed
        if mic is None:
            return
        audio = mic.convert(samples, rate)
        if self._audio is not None and self._audio.bridge is not None:
            audio = self._audio.bridge.mix_buffer.mix(audio)
        if audio:
            self.get_event_loop().call_soon_threadsafe(self._offer_local, audio)

    def _offer_local(self, pcm: bytes) -> None:  # event loop
        if self._ear.accept(LOCAL, pcm):
            asyncio.create_task(
                self.push_audio_frame(
                    InputAudioRawFrame(audio=pcm, sample_rate=self.sample_rate, num_channels=1)
                )
            )


class MacVoiceOutputTransport(BaseOutputTransport):
    """Speech to this Mac's speaker or the paired Mac's, whichever `Ear` says is live."""

    def __init__(
        self,
        audio: MacVoiceAudio | None,
        params: TransportParams,
        *,
        hub: "RelayHub | None" = None,
        ear: Ear | None = None,
    ) -> None:
        super().__init__(params)
        self._audio = audio
        self._hub = hub
        self._ear = ear or Ear(local=audio is not None)
        self._until = 0.0  # loop time when everything queued so far has played

    async def setup(self, setup: FrameProcessorSetup) -> None:  # type: ignore[override]  # see the input side
        await super().setup(setup)
        if self._audio is not None:
            self._audio.connect_player(self.sample_rate)

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if self._audio is not None:
            self._audio.start()
            if self._audio.meet_voice is not None:
                await self._audio.meet_voice.start()
        await self.set_transport_ready(frame)

    async def cleanup(self) -> None:
        await super().cleanup()
        if self._audio is not None and self._audio.meet_voice is not None:
            await self._audio.meet_voice.stop()

    @property
    def _remote(self) -> bool:
        return self._ear.active == RELAY and self._hub is not None and self._hub.client is not None

    def handoff(self) -> None:
        """The mic and speaker just changed Macs: what was queued for the old one no longer holds the next frame."""
        self._until = 0.0

    async def process_frame(self, frame: Any, direction: Any) -> None:
        if isinstance(frame, InterruptionFrame):
            await self._interrupt()
        await super().process_frame(frame, direction)

    async def _interrupt(self) -> None:
        """The user talked over Jarvis: what is still queued on the Mac that is speaking is no longer wanted."""
        self._until = 0.0
        if self._hub is not None:
            await self._hub.flush()
        if self._audio is not None:
            self._audio.flush()

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        # The player queues instantly; waiting here paces the output like a blocking speaker write, so an
        # interruption drops the audio Pipecat still holds instead of letting everything already queued play out.
        now = asyncio.get_running_loop().time()
        speech = isinstance(frame, TTSAudioRawFrame)
        audio = self._audio
        if self._remote:
            assert self._hub is not None
            seconds = await self._hub.send_audio(frame.audio, sample_rate=frame.sample_rate)
            if audio is not None and audio.meet_voice is not None and speech:
                audio.meet_voice.send(frame.audio)  # the call still hears Jarvis when the mic is another Mac's
        elif audio is not None:
            seconds = audio.play(frame.audio, speech=speech)
        else:
            # No local speaker and no paired Mac yet: keep the pace as if there were, so the pipeline never stalls.
            seconds = len(frame.audio) / 2 / max(1, frame.sample_rate)
        self._ear.spoke(seconds)
        self._until = max(self._until, now) + seconds
        await asyncio.sleep(max(0.0, self._until - now - PLAY_AHEAD_SECS))
        return True


class MacVoiceTransport(BaseTransport):
    """
    The mic and speaker, like Pipecat's LocalAudioTransport, but with Apple's echo canceller in the path.

    They are this Mac's unless a paired Mac is connected as the thin client (`hub`): that one owns both while it is
    connected, and hands them back when it goes. With no local audio hardware (a Mac mini with no mic), `has_local_audio`
    is False and Jarvis keeps quiet until a paired Mac connects.
    """

    def __init__(
        self,
        params: TransportParams,
        *,
        meeting: bool = False,
        hub: "RelayHub | None" = None,
        ear: Ear | None = None,
    ) -> None:
        super().__init__()
        self._params = params
        self._hub = hub
        self._audio: MacVoiceAudio | None = None
        try:
            self._audio = MacVoiceAudio(
                bridge=MeetingAudioBridge() if meeting else None,  # hears the call
                meet_voice=MeetVoice() if meeting else None,  # speaks into it
            )
            if not self._audio.has_mic_input():
                raise RuntimeError("this Mac has no microphone input")
            logger.info("Mic and speaker run through macOS voice processing (echo cancellation).")
        except Exception as exc:
            # A Mac mini has no mic: voice processing can't start, and a paired Mac is the only ear there is.
            logger.warning(
                f"No local mic or speaker ({exc!r}); Jarvis hears and speaks through a paired Mac when one connects."
            )
            if self._audio is not None:
                self._audio.stop()
            self._audio = None
        # The relay owns the ear when one is passed in (a paired Mac's audio and this Mac's feed the same one).
        self._ear = ear or Ear(local=self.has_local_audio)
        self._ear.set_local(self.has_local_audio)
        self._ear.on_switch = self._ear_switched
        self._input: MacVoiceInputTransport | None = None
        self._output: MacVoiceOutputTransport | None = None
        if hub is not None:
            hub.on_attach.append(self._remote_attached)
            hub.on_detach.append(self._remote_detached)

    @property
    def has_local_audio(self) -> bool:
        return self._audio is not None

    def _ear_switched(self, old: str, new: str) -> None:
        """The live mic and speaker moved: stop what the old one queued and tell the paired Mac where it is."""
        if self._output is not None:
            self._output.handoff()
        if old == LOCAL and self._audio is not None:
            self._audio.flush()
        if self._hub is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            if old == RELAY:
                loop.create_task(self._hub.flush())
            loop.create_task(self._hub.send_ear(new == RELAY))

    def _remote_attached(self) -> None:
        """A paired Mac took the mic and speaker: stop talking here at once, and start its pacing fresh."""
        if self._output is not None:
            self._output.handoff()
        if self._audio is not None:
            self._audio.flush()
            if settings.master_check:
                logger.warning(
                    "MASTER_CHECK is on and the mic is a paired Mac's now: the voiceprint was recorded with this "
                    "Mac's, so your sentences may score low. Watch the [Speaker] score= lines; lower "
                    "SPEAKER_THRESHOLD if they are dropped."
                )

    def _remote_detached(self) -> None:
        if self._output is not None:
            self._output.handoff()

    def input(self) -> FrameProcessor:
        if not self._input:
            self._input = MacVoiceInputTransport(self._audio, self._params, hub=self._hub, ear=self._ear)
        return self._input

    def output(self) -> FrameProcessor:
        if not self._output:
            self._output = MacVoiceOutputTransport(self._audio, self._params, hub=self._hub, ear=self._ear)
        return self._output

    async def set_meeting(self, on: bool) -> None:
        """
        "Meeting mode on" / "meeting mode off" by voice, without rebuilding the transport.

        On: start hearing the call (system audio tap) and speaking into it (the Meet extension's socket). Off: stop
        both. Each half is best effort — a denied Screen & System Audio permission logs a warning and the rest of
        meeting mode still works.
        """
        audio = self._audio
        if audio is None:
            logger.warning("Meeting mode needs this Mac's audio hardware to hear the call; it is not available here.")
            return
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
