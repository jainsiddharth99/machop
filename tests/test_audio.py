"""System audio: layout, framing, silence, and whether it still sounds right."""

import math

import av
import numpy as np
import pytest

try:
    av.codec.Codec("libopus", "w")
except Exception:
    pytest.skip("this FFmpeg build has no Opus encoder", allow_module_level=True)

from machop.audio import (
    FRAME_SAMPLES,
    SAMPLE_RATE,
    SILENCE_HOLD_FRAMES,
    MAX_QUEUED_FRAMES,
    OpusEncoder,
    SystemAudioSource,
    interleave,
)


def tone(seconds, hz=440.0, amplitude=0.3, channels=2):
    """Interleaved float32, the shape the encoder is fed."""
    count = int(SAMPLE_RATE * seconds)
    signal = (amplitude * np.sin(2 * np.pi * hz * np.arange(count) / SAMPLE_RATE))
    return np.repeat(signal.astype(np.float32), channels)


def test_interleave_puts_the_channels_back_together():
    """ScreenCaptureKit hands over planar audio; libopus wants interleaved."""
    planar = np.array([[1, 2, 3], [4, 5, 6]], dtype=np.float32)
    assert list(interleave(planar, 2)) == [1, 4, 2, 5, 3, 6]


def test_mono_needs_no_interleaving():
    planar = np.array([[1, 2, 3]], dtype=np.float32)
    assert list(interleave(planar, 1)) == [1, 2, 3]


def test_the_result_is_contiguous():
    """`.tobytes()` on a transposed view would silently copy in the wrong order on some numpy versions; contiguity is what makes it safe."""
    planar = np.array([[1, 2, 3], [4, 5, 6]], dtype=np.float32)
    assert interleave(planar, 2).flags["C_CONTIGUOUS"]


def test_one_packet_per_twenty_milliseconds():
    encoder = OpusEncoder()
    packets = encoder.encode(tone(0.1))
    assert len(packets) == 5
    assert [ts for ts, _ in packets] == [0, 20000, 40000, 60000, 80000]
    encoder.close()


def test_a_partial_block_is_held_over_rather_than_dropped():
    """ScreenCaptureKit happens to deliver exactly 960 samples, but nothing promises that."""
    encoder = OpusEncoder()
    half = FRAME_SAMPLES
    assert encoder.encode(np.zeros(half, np.float32)) == []
    assert len(encoder.encode(tone(0.01))) == 1
    encoder.close()


def test_timestamps_advance_on_the_sample_clock_not_the_wall_clock():
    """A wall clock drifts against the audio and the two slide apart."""
    encoder = OpusEncoder()
    stamps = [ts for ts, _ in encoder.encode(tone(0.2))]
    gaps = {b - a for a, b in zip(stamps, stamps[1:])}
    assert gaps == {FRAME_SAMPLES * 1_000_000 // SAMPLE_RATE}
    encoder.close()


def test_silence_stops_costing_anything():
    """A Mac playing nothing should send nothing."""
    encoder = OpusEncoder()
    encoder.encode(tone(0.05))
    sent = len(encoder.encode(np.zeros(int(SAMPLE_RATE * 2) * 2, np.float32)))
    assert sent <= SILENCE_HOLD_FRAMES + 1, (
        f"{sent} packets of silence; it should stop after "
        f"{SILENCE_HOLD_FRAMES} frames"
    )
    encoder.close()


def test_sound_comes_back_the_instant_it_returns():
    """The hold is a grace period, not a latch."""
    encoder = OpusEncoder()
    encoder.encode(np.zeros(int(SAMPLE_RATE * 2) * 2, np.float32))
    assert len(encoder.encode(tone(0.02))) == 1
    encoder.close()


def test_the_encoder_holds_its_bitrate_ceiling():
    """Plain `vbr=on` overshoots: measured 126 kbps against a 96 kbps target on this exact signal."""
    encoder = OpusEncoder(bitrate=96_000)
    packets = encoder.encode(tone(2.0, amplitude=0.7))
    total = sum(len(data) for _, data in packets)
    kbps = total * 8 / (len(packets) * 0.02) / 1000
    encoder.close()
    assert kbps <= 96 * 1.15, f"{kbps:.1f} kbps against a 96 kbps ceiling"


def test_the_tone_survives_the_round_trip():
    """Encode 440 Hz, decode it, and check it is still 440 Hz."""
    encoder = OpusEncoder()
    packets = encoder.encode(tone(1.0, hz=440.0))
    encoder.close()
    assert packets

    decoder = av.CodecContext.create("libopus", "r")
    decoder.sample_rate = SAMPLE_RATE
    decoder.layout = "stereo"
    blocks = []
    for _ts, data in packets:
        for frame in decoder.decode(av.Packet(data)):
            blocks.append(frame.to_ndarray().reshape(-1).astype(np.float64))
    assert blocks, "nothing decoded"

    pcm = np.concatenate(blocks)
    left = pcm[0::2]
    segment = left[SAMPLE_RATE // 4 : SAMPLE_RATE // 4 + SAMPLE_RATE // 2]
    assert segment.size > 1024, "not enough audio decoded to measure"
    spectrum = np.abs(np.fft.rfft(segment * np.hanning(segment.size)))
    peak = float(np.fft.rfftfreq(segment.size, 1 / SAMPLE_RATE)[int(np.argmax(spectrum))])
    assert math.isclose(peak, 440.0, abs_tol=8.0), (
        f"decoded audio peaks at {peak:.1f} Hz, not 440 Hz"
    )


def test_both_channels_come_out_where_they_went_in():
    """A layout mistake shows up here and nowhere else: swapped or summed channels encode to exactly the same number of exactly-sized packets."""
    count = SAMPLE_RATE // 2
    signal = (0.4 * np.sin(2 * np.pi * 440 * np.arange(count) / SAMPLE_RATE))
    stereo = np.zeros(count * 2, np.float32)
    stereo[0::2] = signal

    encoder = OpusEncoder()
    packets = encoder.encode(stereo)
    encoder.close()

    decoder = av.CodecContext.create("libopus", "r")
    decoder.sample_rate = SAMPLE_RATE
    decoder.layout = "stereo"
    blocks = [
        frame.to_ndarray().reshape(-1).astype(np.float64)
        for _ts, data in packets
        for frame in decoder.decode(av.Packet(data))
    ]
    pcm = np.concatenate(blocks)
    left_energy = float(np.sqrt((pcm[0::2] ** 2).mean()))
    right_energy = float(np.sqrt((pcm[1::2] ** 2).mean()))
    assert left_energy > right_energy * 4, (
        f"left {left_energy:.4f} vs right {right_energy:.4f}: the channels "
        f"are not where they were put"
    )


async def test_the_queue_drops_the_oldest_audio_not_the_newest():
    """Late audio is worse than missing audio: it desynchronises from the picture and stays that way, whereas a dropped block is one click."""
    source = SystemAudioSource()
    source._loop = None
    for value in range(MAX_QUEUED_FRAMES + 5):
        source._publish(np.full(4, float(value), np.float32))
    assert source.dropped == 5
    first = await source.read()
    assert first[0] == 5.0, "the queue kept stale audio and threw away fresh"


async def test_a_waiting_reader_is_woken_by_new_audio():
    import asyncio

    source = SystemAudioSource()
    source._loop = asyncio.get_running_loop()
    task = asyncio.create_task(source.read())
    await asyncio.sleep(0)
    source._publish(np.array([1.0, 2.0], np.float32))
    got = await asyncio.wait_for(task, timeout=2.0)
    assert list(got) == [1.0, 2.0]
