"""WebRTC media: the capture source presented as an aiortc video track."""

from __future__ import annotations

import asyncio
import fractions
import logging
import time

import av
from aiortc import RTCRtpSender, RTCRtpTransceiver
from aiortc.codecs import h264
from aiortc.mediastreams import VideoStreamTrack

from .capture import FrameSource

logger = logging.getLogger(__name__)

VIDEO_CLOCK_RATE = 90000
VIDEO_TIME_BASE = fractions.Fraction(1, VIDEO_CLOCK_RATE)

REFINE_INTERVAL_SECONDS = 1 / 15
REFINE_FRAMES = 15

IDLE_REPEAT_SECONDS = 1.0

MIN_BITRATE_FLOOR = 1_000_000
MIN_BITRATE_CEILING = 2_000_000


def bitrate_floor(ceiling: int) -> int:
    """The lowest bitrate worth honouring for a given profile ceiling."""
    return max(MIN_BITRATE_FLOOR, min(MIN_BITRATE_CEILING, ceiling // 4))


def tune_encoder(max_bitrate: int, min_bitrate: int | None = None) -> None:
    """Set aiortc's H.264 bitrate window for screen content."""
    floor = bitrate_floor(max_bitrate) if min_bitrate is None else min_bitrate
    floor = min(floor, max_bitrate)
    h264.MAX_BITRATE = max_bitrate
    h264.DEFAULT_BITRATE = max_bitrate
    h264.MIN_BITRATE = floor
    logger.debug(
        "H.264 bitrate window %.1f-%.1f Mbps", floor / 1e6, max_bitrate / 1e6
    )


def prefer_h264(transceiver: RTCRtpTransceiver) -> bool:
    """Pin the transceiver to H.264. Returns False if the peer lacks it."""
    capabilities = RTCRtpSender.getCapabilities("video")
    if capabilities is None:
        return False
    h264_codecs = [c for c in capabilities.codecs if c.mimeType == "video/H264"]
    other = [
        c
        for c in capabilities.codecs
        if c.mimeType in ("video/rtx", "video/red", "video/ulpfec")
    ]
    if not h264_codecs:
        logger.warning("No H.264 support in this aiortc build; falling back to VP8")
        return False
    transceiver.setCodecPreferences(h264_codecs + other)
    return True


class ScreenTrack(VideoStreamTrack):
    """Presents a FrameSource to aiortc, paced on a real 90 kHz clock."""

    kind = "video"

    def __init__(self, source: FrameSource) -> None:
        super().__init__()
        self._source = source
        self._start: float | None = None
        self._last_frame: av.VideoFrame | None = None
        self._idle_repeats = 0

    async def recv(self) -> av.VideoFrame:
        frame = await self._next_frame()

        now = time.monotonic()
        if self._start is None:
            self._start = now
        frame.pts = int((now - self._start) * VIDEO_CLOCK_RATE)
        frame.time_base = VIDEO_TIME_BASE
        return frame

    async def _next_frame(self) -> av.VideoFrame:
        """Freshest frame, or a repeat of the last one when nothing changed."""
        timeout = (
            REFINE_INTERVAL_SECONDS
            if self._idle_repeats < REFINE_FRAMES
            else IDLE_REPEAT_SECONDS
        )
        try:
            frame = await asyncio.wait_for(self._source.read(), timeout=timeout)
        except asyncio.TimeoutError:
            if self._last_frame is None:
                return await self._source.read()
            self._idle_repeats += 1
            return self._last_frame
        self._idle_repeats = 0
        self._last_frame = frame
        return frame

    def stop(self) -> None:
        super().stop()


STOCK_H264_ENCODER = h264.H264Encoder


class TunedH264Encoder(h264.H264Encoder):
    """aiortc's encoder with x264 settings chosen for screen content."""

    def _encode_frame(self, frame: av.VideoFrame, force_keyframe: bool):
        if self.codec is not None:
            if (frame.width, frame.height) != (self.codec.width, self.codec.height):
                self.codec = None
            else:
                self.codec.bit_rate = self.target_bitrate
        if self.codec is None:
            self.codec = self._tuned_context(frame)
        return super()._encode_frame(frame, force_keyframe)

    def _tuned_context(self, frame: av.VideoFrame) -> av.CodecContext:
        context = av.CodecContext.create("libx264", "w")
        context.width = frame.width
        context.height = frame.height
        context.bit_rate = self.target_bitrate
        context.pix_fmt = "nv12"
        context.framerate = fractions.Fraction(h264.MAX_FRAME_RATE, 1)
        context.time_base = fractions.Fraction(1, h264.MAX_FRAME_RATE)
        context.profile = "Baseline"
        context.options = {
            "level": "31",
            "preset": "ultrafast",
            "tune": "zerolatency",
        }
        return context


def install_tuned_encoder() -> None:
    """Swap the encoder aiortc dispatches to."""
    import aiortc.codecs

    if aiortc.codecs.H264Encoder is TunedH264Encoder:
        return
    aiortc.codecs.H264Encoder = TunedH264Encoder
    h264.H264Encoder = TunedH264Encoder
    logger.debug("Installed x264 ultrafast/zerolatency encoder")
