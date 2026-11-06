"""
Mic and speaker through macOS voice processing, the echo canceller FaceTime uses, so Jarvis hears the user over
its own voice on speakers. Apple cancels only what plays through the same AVAudioEngine, so one engine owns both.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import numpy as np
import soxr
from loguru import logger
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameProcessor, FrameProcessorSetup
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

PLAY_AHEAD_SECS = 0.08  # audio queued in the player beyond what is playing; an interruption cuts in after this
SILENT_MIC_SECS = 3.0  # this long after start without real sound means the mic is not reaching Jarvis
SILENT_MIC_PEAK = 1e-3  # -60 dBFS; a quiet room through voice processing peaks near 1e-2
SILENT_MIC_WARNING = (
    "Mic is silent: macOS voice processing delivers no sound. Set System Settings > Sound > Output to speakers "
    "or headphones; a Multi-Output or aggregate device (BlackHole and similar) stops the mic."
)


class MicConverter:
    """The processed mic (float32, 48 kHz on Macs) as 16-bit PCM at Jarvis's rate, one stream per input rate."""

    def __init__(self, out_rate: int) -> None:
        self.out_rate = out_rate
        self.heard = False  # any block loud enough to be a real mic, not a dead one
        self._rate = 0
        self._stream: soxr.ResampleStream | None = None

    def convert(self, samples: np.ndarray, rate: int) -> bytes:
        self.heard = self.heard or (len(samples) > 0 and float(np.abs(samples).max()) >= SILENT_MIC_PEAK)
        if rate != self._rate:
            self._stream, self._rate = soxr.ResampleStream(rate, self.out_rate, 1, dtype="float32"), rate
        out = self._stream.resample_chunk(samples) if rate != self.out_rate else samples
        return (np.clip(out, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


class MacVoiceAudio:
    """One AVAudioEngine with voice processing on: the mic tap and the player share it. One per process."""

    def __init__(self) -> None:
        import AVFoundation  # imported here: macOS only, and slow to load

        self._av = AVFoundation
        self.engine = AVFoundation.AVAudioEngine.alloc().init()
        # The output side must exist before voice processing is turned on, or the engine fails to start (-10875).
        self.engine.outputNode(), self.engine.mainMixerNode()
        ok, err = self.engine.inputNode().setVoiceProcessingEnabled_error_(True, None)
        if not ok:
            raise RuntimeError(f"macOS voice processing could not start: {err}")
        self.player = AVFoundation.AVAudioPlayerNode.alloc().init()
        self.engine.attachNode_(self.player)
        self._play_format = None
        self._tapped = False

    def on_mic(self, callback: Callable[[np.ndarray, int], None]) -> None:
        """Call `callback(samples, rate)` from the audio thread with each block of the processed mic."""
        mic = self.engine.inputNode()
        fmt = mic.outputFormatForBus_(0)  # several identical channels with voice processing on; channel 0 is enough
        rate = int(fmt.sampleRate())

        def tap(buffer, when) -> None:  # an Objective-C void block: it must return None
            n = buffer.frameLength()
            callback(np.frombuffer(buffer.floatChannelData()[0].as_buffer(n), dtype=np.float32).copy(), rate)

        mic.installTapOnBus_bufferSize_format_block_(0, rate // 50, fmt, tap)  # asks for 20 ms blocks
        self._tapped = True

    def connect_player(self, rate: int) -> None:
        """Play mono audio at `rate`; the mixer converts it to the speaker's rate."""
        av = self._av
        self._play_format = av.AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
            av.AVAudioPCMFormatFloat32, float(rate), 1, False
        )
        self.engine.connect_to_format_(self.player, self.engine.mainMixerNode(), self._play_format)

    def start(self) -> None:
        if not self.engine.isRunning():
            ok, err = self.engine.startAndReturnError_(None)
            if not ok:
                raise RuntimeError(f"audio engine could not start: {err}")
        if self._play_format is not None and not self.player.isPlaying():
            self.player.play()

    def play(self, pcm16: bytes) -> float:
        """Queue 16-bit mono audio; returns its length in seconds. It plays after what is already queued."""
        samples = np.frombuffer(pcm16, dtype=np.int16)
        if self._play_format is None or not len(samples):
            return 0.0
        buffer = self._av.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(self._play_format, len(samples))
        np.frombuffer(buffer.floatChannelData()[0].as_buffer(len(samples)), dtype=np.float32)[:] = samples / 32768
        buffer.setFrameLength_(len(samples))
        self.player.scheduleBuffer_completionHandler_(buffer, None)
        return len(samples) / self._play_format.sampleRate()

    def stop(self) -> None:
        if self._tapped:
            self.engine.inputNode().removeTapOnBus_(0)
            self._tapped = False
        self.player.stop()
        self.engine.stop()


class MacVoiceInputTransport(BaseInputTransport):
    def __init__(self, audio: MacVoiceAudio, params: TransportParams) -> None:
        super().__init__(params)
        self._audio = audio
        self._mic: MicConverter | None = None
        self._silence_check: asyncio.Task | None = None

    async def setup(self, setup: FrameProcessorSetup) -> None:
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
        if not self._mic.heard:
            logger.error(SILENT_MIC_WARNING)

    def _on_mic(self, samples: np.ndarray, rate: int) -> None:  # audio thread
        audio = self._mic.convert(samples, rate)
        if audio:
            frame = InputAudioRawFrame(audio=audio, sample_rate=self._mic.out_rate, num_channels=1)
            asyncio.run_coroutine_threadsafe(self.push_audio_frame(frame), self.get_event_loop())


class MacVoiceOutputTransport(BaseOutputTransport):
    def __init__(self, audio: MacVoiceAudio, params: TransportParams) -> None:
        super().__init__(params)
        self._audio = audio
        self._until = 0.0  # loop time when everything queued so far has played

    async def setup(self, setup: FrameProcessorSetup) -> None:
        await super().setup(setup)
        self._audio.connect_player(self.sample_rate)

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        self._audio.start()
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        # The player queues instantly; waiting here paces the output like a blocking speaker write, so an
        # interruption drops the audio Pipecat still holds instead of letting everything already queued play out.
        now = asyncio.get_running_loop().time()
        self._until = max(self._until, now) + self._audio.play(frame.audio)
        await asyncio.sleep(max(0.0, self._until - now - PLAY_AHEAD_SECS))
        return True


class MacVoiceTransport(BaseTransport):
    """Local mic and speaker, like Pipecat's LocalAudioTransport, but with Apple's echo canceller in the path."""

    def __init__(self, params: TransportParams) -> None:
        super().__init__()
        self._params = params
        self._audio = MacVoiceAudio()
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
