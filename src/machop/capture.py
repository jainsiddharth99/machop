"""Screen capture."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import av
import numpy as np

logger = logging.getLogger(__name__)

PIXEL_FORMAT_NV12 = 0x34323076

_OUTPUT_CLASS = None


def _stream_output_class():
    """Define the SCStreamOutput delegate class exactly once."""
    global _OUTPUT_CLASS
    if _OUTPUT_CLASS is None:
        from Foundation import NSObject

        class _StreamOutput(NSObject):
            def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, stype):
                source = getattr(self, "source", None)
                if source is not None:
                    source._on_sample(sbuf)

        _OUTPUT_CLASS = _StreamOutput
    return _OUTPUT_CLASS


@dataclass(frozen=True)
class DisplayGeometry:
    """Display size in *points*, which is the unit CGEvent expects."""

    width: int
    height: int


class FrameSource(ABC):
    """A source of encoder-ready video frames."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def read(self) -> av.VideoFrame:
        """Await the next frame. Returns the freshest frame, never a backlog."""


def _describe(error: object) -> str:
    """Readable text for an NSError."""
    if error is None:
        return "no detail given"
    for attribute in ("localizedDescription", "localizedFailureReason"):
        getter = getattr(error, attribute, None)
        if callable(getter):
            try:
                text = getter()
            except Exception:
                continue
            if text:
                domain = getattr(error, "domain", None)
                code = getattr(error, "code", None)
                suffix = ""
                if callable(domain) and callable(code):
                    try:
                        suffix = f" [{domain()} {code()}]"
                    except Exception:
                        suffix = ""
                return f"{text}{suffix}"
    text = str(error).strip()
    return text or repr(error)


def _nv12_to_frame(
    y: np.ndarray, uv: np.ndarray, width: int, height: int
) -> av.VideoFrame:
    """Copy NV12 planes into an av.VideoFrame, honouring PyAV's own stride."""
    frame = av.VideoFrame(width, height, "nv12")
    for plane, src in ((frame.planes[0], y), (frame.planes[1], uv)):
        stride = plane.line_size
        if stride == src.shape[1]:
            plane.update(src.tobytes())
        else:
            padded = np.zeros((src.shape[0], stride), dtype=np.uint8)
            padded[:, : src.shape[1]] = src
            plane.update(padded.tobytes())
    return frame


class ScreenCaptureKitSource(FrameSource):
    """SCStream-backed capture. Frames arrive on a dispatch queue thread."""

    def __init__(self, width: int, height: int, fps: int = 60, show_cursor: bool = True):
        super().__init__(width, height)
        self.fps = fps
        self.show_cursor = show_cursor
        self._stream = None
        self._output = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._latest: av.VideoFrame | None = None
        self._new_frame = asyncio.Event()
        self._frames_seen = 0

    async def start(self) -> None:
        import CoreMedia
        import ScreenCaptureKit as SCK

        self._loop = asyncio.get_running_loop()
        display = await self._first_display()

        config = SCK.SCStreamConfiguration.alloc().init()
        config.setWidth_(self.width)
        config.setHeight_(self.height)
        config.setPixelFormat_(PIXEL_FORMAT_NV12)
        config.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, self.fps))
        config.setQueueDepth_(3)
        config.setShowsCursor_(self.show_cursor)

        content_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
            display, []
        )
        self._stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
            content_filter, config, None
        )
        self._output = _stream_output_class().alloc().init()
        self._output.source = self
        ok, err = self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self._output, 0, None, None
        )
        if not ok:
            raise RuntimeError(f"Could not attach capture output: {err}")

        await self._call_with_handler(self._stream.startCaptureWithCompletionHandler_)
        logger.debug("ScreenCaptureKit stream started at %dx%d", self.width, self.height)

    async def _first_display(self):
        import ScreenCaptureKit as SCK

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def handler(content, error):
            def resolve():
                if future.done():
                    return
                if error is not None or content is None:
                    future.set_exception(
                        RuntimeError(f"Cannot enumerate displays: {_describe(error)}")
                    )
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

    async def _call_with_handler(self, method) -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def handler(error):
            def resolve():
                if future.done():
                    return
                if error is not None:
                    future.set_exception(RuntimeError(_describe(error)))
                else:
                    future.set_result(None)

            try:
                loop.call_soon_threadsafe(resolve)
            except RuntimeError:
                pass

        method(handler)
        await asyncio.wait_for(future, timeout=10.0)

    def _on_sample(self, sbuf) -> None:
        """Runs on a dispatch queue thread. Must never raise into ObjC."""
        import CoreMedia
        import Quartz as CV

        try:
            if not CoreMedia.CMSampleBufferIsValid(sbuf):
                return
            pixel_buffer = CoreMedia.CMSampleBufferGetImageBuffer(sbuf)
            if pixel_buffer is None:
                return

            CV.CVPixelBufferLockBaseAddress(pixel_buffer, 1)
            try:
                width = CV.CVPixelBufferGetWidth(pixel_buffer)
                height = CV.CVPixelBufferGetHeight(pixel_buffer)
                y_ptr = CV.CVPixelBufferGetBaseAddressOfPlane(pixel_buffer, 0)
                y_stride = CV.CVPixelBufferGetBytesPerRowOfPlane(pixel_buffer, 0)
                uv_ptr = CV.CVPixelBufferGetBaseAddressOfPlane(pixel_buffer, 1)
                uv_stride = CV.CVPixelBufferGetBytesPerRowOfPlane(pixel_buffer, 1)

                y = np.frombuffer(y_ptr.as_buffer(y_stride * height), np.uint8)
                y = y.reshape(height, y_stride)[:, :width]
                uv = np.frombuffer(uv_ptr.as_buffer(uv_stride * (height // 2)), np.uint8)
                uv = uv.reshape(height // 2, uv_stride)[:, :width]

                frame = _nv12_to_frame(y, uv, width, height)
            finally:
                CV.CVPixelBufferUnlockBaseAddress(pixel_buffer, 1)

            self._publish(frame)
        except Exception:
            logger.exception("Dropping frame: capture callback failed")

    def _publish(self, frame: av.VideoFrame) -> None:
        with self._lock:
            self._latest = frame
            self._frames_seen += 1
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._new_frame.set)
        except RuntimeError:
            pass

    async def read(self) -> av.VideoFrame:
        """Latest-frame-wins: a stale frame is worthless in a live mirror."""
        while True:
            with self._lock:
                frame = self._latest
                self._latest = None
            if frame is not None:
                return frame
            self._new_frame.clear()
            await self._new_frame.wait()

    def peek(self) -> av.VideoFrame | None:
        """Most recent frame without consuming it (for idle repeats)."""
        with self._lock:
            return self._latest

    @property
    def frames_seen(self) -> int:
        with self._lock:
            return self._frames_seen

    async def stop(self) -> None:
        if self._stream is None:
            return
        stream, self._stream = self._stream, None
        try:
            await self._call_with_handler(stream.stopCaptureWithCompletionHandler_)
        except Exception as exc:
            logger.debug("Capture stream stop reported: %s", exc)
        finally:
            self._output = None


class QuartzSource(FrameSource):
    """CGDisplayCreateImage fallback. Deprecated API, ~30x the CPU cost."""

    def __init__(self, width: int, height: int, fps: int = 30):
        super().__init__(width, height)
        self.fps = fps
        self._interval = 1.0 / fps
        self._next_deadline = 0.0

    async def start(self) -> None:
        logger.warning(
            "Falling back to CGDisplayCreateImage; expect higher CPU use and lower frame rate"
        )

    async def stop(self) -> None:
        return None

    async def read(self) -> av.VideoFrame:
        now = time.monotonic()
        if now < self._next_deadline:
            await asyncio.sleep(self._next_deadline - now)
        self._next_deadline = max(time.monotonic(), self._next_deadline + self._interval)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._grab)

    def _grab(self) -> av.VideoFrame:
        import objc
        import Quartz

        with objc.autorelease_pool():
            image = Quartz.CGDisplayCreateImage(Quartz.CGMainDisplayID())
            if image is None:
                raise RuntimeError(
                    "Screen capture returned nothing - is Screen Recording permission granted?"
                )
            width = Quartz.CGImageGetWidth(image)
            height = Quartz.CGImageGetHeight(image)
            stride = Quartz.CGImageGetBytesPerRow(image)
            raw = bytes(
                Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image))
            )

        usable = np.frombuffer(raw[: height * stride], dtype=np.uint8)
        pixels = usable.reshape(height, stride)[:, : width * 4].reshape(height, width, 4)
        frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(pixels), format="bgra")
        return frame.reformat(width=self.width, height=self.height, format="nv12")


def display_geometry() -> DisplayGeometry:
    """Main display size in points - the coordinate space CGEvent uses."""
    import Quartz

    display_id = Quartz.CGMainDisplayID()
    return DisplayGeometry(
        width=int(Quartz.CGDisplayPixelsWide(display_id)),
        height=int(Quartz.CGDisplayPixelsHigh(display_id)),
    )


MACROBLOCK = 16


def target_size(geometry: DisplayGeometry, max_width: int) -> tuple[int, int]:
    """Capture size preserving aspect ratio, aligned to whole macroblocks."""
    width = min(max_width, geometry.width)
    height = round(width * geometry.height / geometry.width)
    width -= width % MACROBLOCK
    height -= height % MACROBLOCK
    return (max(width, MACROBLOCK), max(height, MACROBLOCK))


async def create_frame_source(
    width: int, height: int, fps: int, show_cursor: bool = True
) -> FrameSource:
    """ScreenCaptureKit if it will start, Quartz otherwise."""
    source: FrameSource = ScreenCaptureKitSource(width, height, fps, show_cursor)
    try:
        await source.start()
        return source
    except Exception as exc:
        logger.warning(
            "ScreenCaptureKit unavailable (%s: %s); falling back",
            type(exc).__name__,
            exc or "no detail",
        )
        fallback = QuartzSource(width, height, min(fps, 30))
        await fallback.start()
        return fallback


PROFILES: dict[str, tuple[int, int]] = {
    "text": (1100, 2_000_000),
    "gui": (1440, 8_000_000),
}


class SwitchableSource(FrameSource):
    """A FrameSource whose resolution can change without being replaced."""

    def __init__(self, width: int, height: int, fps: int, show_cursor: bool = True):
        super().__init__(width, height)
        self.fps = fps
        self.show_cursor = show_cursor
        self._inner: FrameSource | None = None
        self._running = False
        self._swap_lock = asyncio.Lock()

    async def start(self) -> None:
        self._inner = await create_frame_source(
            self.width, self.height, self.fps, self.show_cursor
        )
        self._running = True

    async def stop(self) -> None:
        self._running = False
        inner, self._inner = self._inner, None
        if inner is not None:
            await inner.stop()

    async def read(self) -> av.VideoFrame:
        while True:
            if not self._running:
                raise RuntimeError("Capture is not running")
            inner = self._inner
            if inner is None:
                await asyncio.sleep(0.05)
                continue
            try:
                return await asyncio.wait_for(inner.read(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

    async def reconfigure(self, width: int, height: int) -> None:
        if (width, height) == (self.width, self.height):
            return
        async with self._swap_lock:
            old, self._inner = self._inner, None
            if old is not None:
                await old.stop()
            self.width, self.height = width, height
            self._inner = await create_frame_source(
                width, height, self.fps, self.show_cursor
            )
            logger.info("Capture reconfigured to %dx%d", width, height)
