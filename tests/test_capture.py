"""Capture tests that touch the real display."""

import pytest

pytest.importorskip("Quartz", reason="macOS only")

from machop.capture import (
    DisplayGeometry,
    ScreenCaptureKitSource,
    create_frame_source,
    display_geometry,
    target_size,
)


def test_display_geometry_is_sane():
    geometry = display_geometry()
    assert geometry.width > 0 and geometry.height > 0


def test_target_size_preserves_aspect_ratio():
    """Within a macroblock."""
    geometry = DisplayGeometry(width=1470, height=956)
    width, height = target_size(geometry, 1440)
    assert width == 1440
    assert abs(width / height - geometry.width / geometry.height) < 0.03


def test_target_size_is_macroblock_aligned():
    """H.264 codes in 16x16 blocks."""
    for max_width in range(300, 1500, 37):
        width, height = target_size(DisplayGeometry(1470, 956), max_width)
        assert width % 16 == 0 and height % 16 == 0, f"{width}x{height}"
        assert width > 0 and height > 0


def test_target_size_never_upscales():
    width, _ = target_size(DisplayGeometry(1470, 956), 4000)
    assert width <= 1470


async def test_captures_a_real_frame():
    """The regression test for the v0.1 reshape crash."""
    source = await create_frame_source(640, 400, 30)
    try:
        frame = await source.read()
    finally:
        await source.stop()
    assert (frame.width, frame.height) == (640, 400)
    assert frame.format.name in ("nv12", "yuv420p")


async def test_screencapturekit_survives_restart():
    """Declaring the ObjC delegate inside start() broke every session after the first with 'overriding existing Objective-C class'."""
    for _ in range(2):
        source = ScreenCaptureKitSource(640, 400, 30)
        await source.start()
        try:
            frame = await source.read()
            assert frame.width == 640
        finally:
            await source.stop()


async def test_read_returns_freshest_frame_not_a_backlog():
    source = await create_frame_source(640, 400, 30)
    try:
        await source.read()
        assert source.peek() is None, "a consumed frame must not linger"
    finally:
        await source.stop()


def test_error_description_never_returns_empty():
    """str(NSError) is often empty, which produced the useless message 'ScreenCaptureKit unavailable ()' when a second instance could not start."""
    from machop.capture import _describe

    class Silent:
        def __str__(self) -> str:
            return ""

    class Nsish:
        def localizedDescription(self):
            return "Screen capture is already in use"

        def domain(self):
            return "SCStreamErrorDomain"

        def code(self):
            return -3801

    assert _describe(None) == "no detail given"
    assert _describe(Silent()) != ""
    described = _describe(Nsish())
    assert "already in use" in described and "-3801" in described


def test_error_description_survives_a_hostile_object():
    """Diagnostics must never raise - they run on a failure path."""
    from machop.capture import _describe

    class Hostile:
        def localizedDescription(self):
            raise RuntimeError("boom")

        def __str__(self) -> str:
            return "fallback text"

    assert _describe(Hostile()) == "fallback text"
