"""
The Mac's mic and speaker through macOS voice processing, the echo canceller FaceTime uses, without Pipecat.

`mac_audio.py` wraps this in a transport; `jarvis enroll` and the thin relay client (`defaults/remote/client.py`)
use it alone. It is a separate module so those two never load the pipeline, the agents or the models.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import soxr

# Audio queued in the player beyond what is playing; an interruption cuts in after this.
PLAY_AHEAD_SECS = 0.08
# A block this loud means a real mic rather than a dead one (-60 dBFS; a quiet room peaks near -40).
SILENT_MIC_PEAK = 1e-3


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
        assert self._stream is not None  # made above on the first block
        out = self._stream.resample_chunk(samples) if rate != self.out_rate else samples
        return (np.clip(out, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


class VoiceAudio:
    """
    One AVAudioEngine with voice processing on: the mic tap and the player share it. One per process.

    Apple cancels only the echo of what plays through the same engine, so the mic and the speaker belong together.
    The meeting extras are passed in rather than made here: this module must stay free of Pipecat.
    """

    def __init__(self, *, bridge: Any = None, meet_voice: Any = None) -> None:
        import AVFoundation  # imported here: macOS only, and slow to load

        self._av = AVFoundation
        self.engine = AVFoundation.AVAudioEngine.alloc().init()
        # The output side must exist before voice processing is turned on, or the engine fails to start (-10875).
        self.engine.outputNode(), self.engine.mainMixerNode()
        ok, err = self.engine.inputNode().setVoiceProcessingEnabled_error_(True, None)
        if not ok:
            raise RuntimeError(f"macOS voice processing could not start: {err}")
        # Voice processing turns down every other app's sound by default; keep that to the least macOS allows.
        self.engine.inputNode().setVoiceProcessingOtherAudioDuckingConfiguration_(
            (False, AVFoundation.AVAudioVoiceProcessingOtherAudioDuckingLevelMin)
        )
        self.player = AVFoundation.AVAudioPlayerNode.alloc().init()
        self.engine.attachNode_(self.player)
        self._play_format = None
        self._tapped = False
        self.bridge = bridge  # hears the call, in a meeting
        self.meet_voice = meet_voice  # speaks into it

    def has_mic_input(self) -> bool:
        """Whether this Mac has an input voice processing can tap: a Mac mini with no mic reports none."""
        try:
            fmt = self.engine.inputNode().outputFormatForBus_(0)
            return int(fmt.sampleRate()) > 0 and int(fmt.channelCount()) > 0
        except Exception:
            return False

    def on_mic(self, callback: Callable[[np.ndarray, int], None]) -> None:
        """Call `callback(samples, rate)` from the audio thread with each block of the processed mic."""
        mic = self.engine.inputNode()
        fmt = mic.outputFormatForBus_(0)  # several identical channels with voice processing on; channel 0 is enough
        rate = int(fmt.sampleRate())
        # A Mac with no input device reports 0 Hz and no channels; `installTapOnBus` would crash on that block size.
        if rate <= 0 or int(fmt.channelCount()) <= 0:
            raise RuntimeError("this Mac has no microphone input for voice processing")

        def tap(buffer, when) -> None:  # an Objective-C void block: it must return None
            n = buffer.frameLength()
            samples = np.frombuffer(buffer.floatChannelData()[0].as_buffer(n), dtype=np.float32).copy()
            callback(samples, rate)

        mic.installTapOnBus_bufferSize_format_block_(0, rate // 50, fmt, tap)  # asks for 20 ms blocks
        self._tapped = True

    def connect_player(self, rate: int) -> None:
        """Play mono audio at `rate`; the mixer converts it to the speaker's rate."""
        av = self._av
        self._play_format = av.AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
            av.AVAudioPCMFormatFloat32, float(rate), 1, False
        )
        self.engine.connect_to_format_(self.player, self.engine.mainMixerNode(), self._play_format)
        if self.meet_voice is not None:
            self.meet_voice.rate = rate

    def start(self) -> None:
        if self.bridge is not None:
            self.bridge.start()
        if not self.engine.isRunning():
            ok, err = self.engine.startAndReturnError_(None)
            if not ok:
                raise RuntimeError(f"audio engine could not start: {err}")
        if self._play_format is not None and not self.player.isPlaying():
            self.player.play()

    def play(self, pcm16: bytes, *, speech: bool = True) -> float:
        """Queue 16-bit mono audio; returns its length in seconds. It plays after what is already queued."""
        samples = np.frombuffer(pcm16, dtype=np.int16)
        if self._play_format is None or not len(samples):
            return 0.0
        buffer = self._av.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(self._play_format, len(samples))
        np.frombuffer(buffer.floatChannelData()[0].as_buffer(len(samples)), dtype=np.float32)[:] = samples / 32768
        buffer.setFrameLength_(len(samples))
        self.player.scheduleBuffer_completionHandler_(buffer, None)
        if self.meet_voice is not None and speech:
            self.meet_voice.send(pcm16)
        return len(samples) / self._play_format.sampleRate()

    def flush(self) -> None:
        """Drop everything queued but not yet played, for an interruption; the next play starts at once."""
        if self._play_format is None:
            return
        self.player.stop()  # also drops the buffers scheduled after the one playing
        self.player.play()

    def stop(self) -> None:
        if self._tapped:
            self.engine.inputNode().removeTapOnBus_(0)
            self._tapped = False
        if self.bridge is not None:
            self.bridge.stop()
        self.player.stop()
        self.engine.stop()
