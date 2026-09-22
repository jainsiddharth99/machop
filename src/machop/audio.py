"""System audio capture and Opus encoding."""

from __future__ import annotations

import asyncio
import fractions
import logging
import threading
from collections import deque

import av
import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_SAMPLES = 960
AUDIO_BITRATE = 96_000

MAX_QUEUED_FRAMES = 12

SILENCE_FLOOR = 1.0 / 32768.0
SILENCE_HOLD_FRAMES = 25

_AUDIO_OUTPUT_CLASS = None

_FLAG_IS_FLOAT = 1 << 0
_FLAG_IS_NON_INTERLEAVED = 1 << 5


def _audio_output_class():
    """Define the audio SCStreamOutput delegate exactly once."""
    global _AUDIO_OUTPUT_CLASS
    if _AUDIO_OUTPUT_CLASS is None:
        from Foundation import NSObject

        class _AudioOutput(NSObject):
            def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, stype):
                source = getattr(self, "source", None)
                if source is not None and stype == 1:
                    source._on_sample(sbuf)

        _AUDIO_OUTPUT_CLASS = _AudioOutput
    return _AUDIO_OUTPUT_CLASS


def interleave(planar: np.ndarray, channels: int) -> np.ndarray:
    """[[L...],[R...]] -> [L,R,L,R,...], which is what libopus wants."""
    if channels == 1:
        return np.ascontiguousarray(planar.reshape(-1), dtype=np.float32)
    return np.ascontiguousarray(planar.T.reshape(-1), dtype=np.float32)


class SystemAudioSource:
    """Whatever this Mac is playing, as interleaved float32 at 48 kHz."""

    def __init__(self, channels: int = CHANNELS) -> None:
        self.channels = channels
        self._stream = None
        self._output = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._queue: deque[np.ndarray] = deque(maxlen=MAX_QUEUED_FRAMES)
        self._ready = asyncio.Event()
        self._dropped = 0
        self._warned_layout = False

    async def start(self) -> None:
        import CoreMedia
        import ScreenCaptureKit as SCK

        self._loop = asyncio.get_running_loop()
        display = await _first_display()

        config = SCK.SCStreamConfiguration.alloc().init()
        config.setWidth_(32)
        config.setHeight_(32)
        config.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, 1))
        config.setQueueDepth_(3)
        config.setCapturesAudio_(True)
        config.setSampleRate_(SAMPLE_RATE)
        config.setChannelCount_(self.channels)
        config.setExcludesCurrentProcessAudio_(True)

        content_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
            display, []
        )
        self._stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
            content_filter, config, None
        )
        self._output = _audio_output_class().alloc().init()
        self._output.source = self
        ok, err = self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self._output, 1, None, None
        )
        if not ok:
            self._stream = self._output = None
            raise RuntimeError(f"Could not attach audio output: {err}")
        await _call_with_handler(self._stream.startCaptureWithCompletionHandler_)
        logger.info("System audio capture started (%d ch)", self.channels)

    def _on_sample(self, sbuf) -> None:
        """Runs on a dispatch queue thread. Must never raise into ObjC."""
        import CoreMedia

        try:
            description = CoreMedia.CMSampleBufferGetFormatDescription(sbuf)
            asbd = CoreMedia.CMAudioFormatDescriptionGetStreamBasicDescription(
                description
            )
            if asbd is None:
                return
            flags, channels = int(asbd[2]), int(asbd[6])
            block = CoreMedia.CMSampleBufferGetDataBuffer(sbuf)
            if block is None:
                return
            length = CoreMedia.CMBlockBufferGetDataLength(block)
            status, raw = CoreMedia.CMBlockBufferCopyDataBytes(block, 0, length, None)
            if status != 0 or not raw:
                return
            if not flags & _FLAG_IS_FLOAT:
                if not self._warned_layout:
                    self._warned_layout = True
                    logger.warning("System audio is not float32; not sending it")
                return

            samples = np.frombuffer(bytes(raw), dtype=np.float32)
            if channels > 1 and flags & _FLAG_IS_NON_INTERLEAVED:
                per_channel = samples.size // channels
                planar = samples[: per_channel * channels].reshape(channels, per_channel)
                samples = interleave(planar, channels)
            self._publish(samples)
        except Exception:
            logger.exception("Dropping audio: capture callback failed")

    def _publish(self, samples: np.ndarray) -> None:
        with self._lock:
            if len(self._queue) == self._queue.maxlen:
                self._dropped += 1
            self._queue.append(samples)
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._ready.set)
        except RuntimeError:
            pass

    async def read(self) -> np.ndarray:
        """The next block of interleaved samples."""
        while True:
            with self._lock:
                if self._queue:
                    return self._queue.popleft()
            self._ready.clear()
            await self._ready.wait()

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    async def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            await _call_with_handler(stream.stopCaptureWithCompletionHandler_)
        except Exception as exc:
            logger.debug("Audio stream stop reported: %s", exc)
        finally:
            self._output = None
            with self._lock:
                self._queue.clear()


class OpusEncoder:
    """Interleaved float32 in, Opus packets out, on a 48 kHz sample clock."""

    def __init__(
        self,
        bitrate: int = AUDIO_BITRATE,
        channels: int = CHANNELS,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        self.channels = channels
        self.sample_rate = sample_rate
        self._context = av.CodecContext.create("libopus", "w")
        self._context.sample_rate = sample_rate
        self._context.format = av.AudioFormat("flt")
        self._context.layout = "stereo" if channels == 2 else "mono"
        self._context.bit_rate = bitrate
        self._context.time_base = fractions.Fraction(1, sample_rate)
        self._context.options = {
            "application": "lowdelay",
            "frame_duration": "20",
            "vbr": "constrained",
        }
        self._pending = np.zeros(0, dtype=np.float32)
        self._samples = 0
        self._quiet_for = 0

    @property
    def codec_string(self) -> str:
        return "opus"

    def encode(self, interleaved: np.ndarray) -> list[tuple[int, bytes]]:
        """Returns (timestamp_microseconds, opus_packet)."""
        block = FRAME_SAMPLES * self.channels
        self._pending = (
            interleaved.astype(np.float32, copy=False)
            if self._pending.size == 0
            else np.concatenate((self._pending, interleaved))
        )
        out: list[tuple[int, bytes]] = []
        while self._pending.size >= block:
            chunk, self._pending = self._pending[:block], self._pending[block:]
            timestamp = self._samples * 1_000_000 // self.sample_rate
            self._samples += FRAME_SAMPLES

            if float(np.abs(chunk).max()) < SILENCE_FLOOR:
                self._quiet_for += 1
                if self._quiet_for > SILENCE_HOLD_FRAMES:
                    continue
            else:
                self._quiet_for = 0

            frame = av.AudioFrame(
                format="flt",
                layout=self._context.layout.name,
                samples=FRAME_SAMPLES,
            )
            frame.planes[0].update(chunk.tobytes())
            frame.sample_rate = self.sample_rate
            frame.pts = self._samples - FRAME_SAMPLES
            frame.time_base = self._context.time_base
            for packet in self._context.encode(frame):
                out.append((timestamp, bytes(packet)))
        return out

    def close(self) -> None:
        try:
            self._context.close()
        except Exception:
            logger.debug("Opus encoder close failed", exc_info=True)


async def _first_display():
    import ScreenCaptureKit as SCK

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def handler(content, error):
        def resolve():
            if future.done():
                return
            if error is not None or content is None:
                future.set_exception(RuntimeError(f"Cannot enumerate displays: {error}"))
            else:
                future.set_result(content)

        try:
            loop.call_soon_threadsafe(resolve)
        except RuntimeError:
            pass

    SCK.SCShareableContent.getShareableContentWithCompletionHandler_(handler)
    content = await asyncio.wait_for(future, timeout=10.0)
    displays = content.displays()
    if not displays:
        raise RuntimeError("No displays available to capture")
    return displays[0]


async def _call_with_handler(method) -> None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def handler(error):
        def resolve():
            if future.done():
                return
            if error is not None:
                future.set_exception(RuntimeError(str(error)))
            else:
                future.set_result(None)

        try:
            loop.call_soon_threadsafe(resolve)
        except RuntimeError:
            pass

    method(handler)
    await asyncio.wait_for(future, timeout=10.0)
