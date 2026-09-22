"""aiortc's H.264 encoder runs x264 at the default `medium` preset."""

import fractions

import av

from machop.media import (
    STOCK_H264_ENCODER,
    TunedH264Encoder,
    install_tuned_encoder,
)


def _frame(width=640, height=400):
    frame = av.VideoFrame(width, height, "yuv420p")
    frame.pts = 0
    frame.time_base = fractions.Fraction(1, 90000)
    return frame


def test_install_replaces_the_encoder_aiortc_dispatches_to():
    from aiortc.codecs import get_encoder
    from aiortc.rtcrtpparameters import RTCRtpCodecParameters

    install_tuned_encoder()
    codec = RTCRtpCodecParameters(
        mimeType="video/H264",
        clockRate=90000,
        payloadType=100,
        parameters={"packetization-mode": "1", "profile-level-id": "42e01f"},
    )
    assert isinstance(get_encoder(codec), TunedH264Encoder)


def test_tuned_context_requests_the_fast_preset():
    """Asserted on the freshly built context: PyAV consumes `options` when the encoder opens, so after the first encode() the dict is empty."""
    context = TunedH264Encoder()._tuned_context(_frame())
    assert context.options.get("preset") == "ultrafast"
    assert context.options.get("tune") == "zerolatency"
    assert context.profile == "Baseline"


def test_resolution_change_rebuilds_the_context():
    """Profile switching relies on this."""
    encoder = TunedH264Encoder()
    list(encoder._encode_frame(_frame(640, 400), force_keyframe=True))
    first = encoder.codec
    list(encoder._encode_frame(_frame(480, 304), force_keyframe=True))
    assert (encoder.codec.width, encoder.codec.height) == (480, 304)
    assert encoder.codec is not first


def test_bitrate_drift_does_not_discard_the_tuned_context():
    """aiortc rebuilds its own context whenever the target bitrate moves more than 10%, which would silently restore the slow defaults."""
    encoder = TunedH264Encoder()
    list(encoder._encode_frame(_frame(), force_keyframe=True))
    original = encoder.codec
    encoder.target_bitrate = int(encoder.target_bitrate * 2)
    list(encoder._encode_frame(_frame(), force_keyframe=False))
    assert encoder.codec is original, "tuned context was discarded"


def test_tuned_encoder_is_much_faster_at_real_settings():
    """Compares against aiortc's own encoder on identical frames."""
    import time

    import numpy as np

    from machop.media import tune_encoder


    width, height = 1440, 936
    tune_encoder(8_000_000)

    rng = np.random.default_rng(7)
    base = (rng.random((height, width, 3)) * 40 + 100).astype(np.uint8)
    frames = []
    for i in range(20):
        shifted = np.roll(base, i * 6, axis=0).copy()
        frame = av.VideoFrame.from_ndarray(shifted, format="rgb24").reformat(
            format="yuv420p"
        )
        frame.pts = i * 3000
        frame.time_base = fractions.Fraction(1, 90000)
        frames.append(frame)

    def elapsed(encoder) -> float:
        encoder.target_bitrate = 8_000_000
        encoder.encode(frames[0], force_keyframe=True)
        started = time.perf_counter()
        for frame in frames[1:]:
            encoder.encode(frame)
        return (time.perf_counter() - started) / (len(frames) - 1)

    stock = elapsed(STOCK_H264_ENCODER())
    tuned = elapsed(TunedH264Encoder())
    print(f"\n  stock {stock * 1000:.2f} ms/frame -> tuned {tuned * 1000:.2f} ms/frame "
          f"({stock / tuned:.1f}x)")
    assert tuned < stock / 2, (
        f"expected a large speedup; got stock {stock * 1000:.2f} ms vs "
        f"tuned {tuned * 1000:.2f} ms"
    )


def test_aiortc_has_no_degradation_preference():
    """Documents why none is set."""
    from aiortc.rtcrtpsender import RTCRtpSender

    assert not hasattr(RTCRtpSender, "degradationPreference")


def test_the_floor_is_raised_off_aiortc_s_camera_default():
    from aiortc.codecs import h264

    from machop.media import tune_encoder

    tune_encoder(8_000_000)
    try:
        assert h264.MAX_BITRATE == 8_000_000
        assert h264.DEFAULT_BITRATE == 8_000_000
        assert h264.MIN_BITRATE == 2_000_000
    finally:
        tune_encoder(3_000_000, 500_000)


def test_the_floor_scales_with_the_profile():
    from machop.media import bitrate_floor

    assert bitrate_floor(8_000_000) == 2_000_000
    assert bitrate_floor(2_000_000) == 1_000_000
    assert bitrate_floor(40_000_000) == 2_000_000


def test_an_explicit_floor_wins():
    from aiortc.codecs import h264

    from machop.media import tune_encoder

    tune_encoder(8_000_000, 600_000)
    try:
        assert h264.MIN_BITRATE == 600_000
    finally:
        tune_encoder(3_000_000, 500_000)


def test_the_floor_can_never_exceed_the_ceiling():
    """Otherwise the clamp inverts and every estimate snaps to the floor."""
    from aiortc.codecs import h264

    from machop.media import tune_encoder

    tune_encoder(1_000_000, 9_000_000)
    try:
        assert h264.MIN_BITRATE == 1_000_000
    finally:
        tune_encoder(3_000_000, 500_000)


async def test_still_screens_get_a_fast_burst_of_repeats_then_a_heartbeat():
    """After motion stops the encoder needs repeats to clean up the blocking a scroll left behind."""
    import asyncio
    import time

    from machop.media import REFINE_FRAMES, REFINE_INTERVAL_SECONDS, ScreenTrack

    class Frozen:
        """Yields one frame, then never again: a screen that stopped moving."""

        def __init__(self):
            self.reads = 0

        async def read(self):
            self.reads += 1
            if self.reads > 1:
                await asyncio.sleep(3600)
            return _frame()

    track = ScreenTrack(Frozen())
    started = time.monotonic()
    for _ in range(REFINE_FRAMES + 1):
        await track.recv()
    elapsed = time.monotonic() - started

    budget = REFINE_INTERVAL_SECONDS * (REFINE_FRAMES + 1) + 0.5
    assert elapsed < budget, (
        f"{REFINE_FRAMES} refinement frames took {elapsed:.2f}s; "
        f"at one per second this would be {REFINE_FRAMES}s"
    )
    assert track._idle_repeats == REFINE_FRAMES


async def test_a_fresh_frame_resets_the_refinement_burst():
    """Otherwise a long session would stop refining after the first stop."""
    import asyncio

    from machop.media import ScreenTrack

    class Intermittent:
        def __init__(self):
            self.allow = True

        async def read(self):
            if self.allow:
                self.allow = False
                return _frame()
            await asyncio.sleep(3600)

    source = Intermittent()
    track = ScreenTrack(source)
    await track.recv()
    assert track._idle_repeats == 0
    await track.recv()
    assert track._idle_repeats == 1
    source.allow = True
    await track.recv()
    assert track._idle_repeats == 0, "new motion must restart the refinement"


async def test_the_first_frame_is_waited_for_however_long_it_takes():
    """There is nothing to repeat yet, so a timeout must not produce None."""
    import asyncio

    from machop.media import ScreenTrack

    class Slow:
        async def read(self):
            await asyncio.sleep(0.25)
            return _frame()

    frame = await ScreenTrack(Slow()).recv()
    assert frame is not None


def test_the_peer_to_peer_encoder_also_knows_its_frame_rate():
    """The relay encoder shipped a 1/90000 time base with no frame rate, so x264 believed it was encoding 90,000 fps and starved every frame of bits."""
    import fractions

    from aiortc.codecs import h264

    from machop.media import TunedH264Encoder

    enc = TunedH264Encoder()
    enc.target_bitrate = 4_000_000
    context = enc._tuned_context(_frame(320, 240))
    assert context.framerate == fractions.Fraction(h264.MAX_FRAME_RATE, 1)
    assert context.time_base == fractions.Fraction(1, h264.MAX_FRAME_RATE)
    assert context.framerate.numerator <= 120, "an implausible frame rate"
