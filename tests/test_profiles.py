"""Switching capture resolution mid-session must not disturb the stream."""

import asyncio
import json

import pytest

pytest.importorskip("Quartz", reason="macOS only")

from machop.capture import PROFILES, SwitchableSource
from machop.inputs import InputController
from machop.security import SessionAuth
from machop.server import MIN_CAPTURE_WIDTH, VIEWPORT_RESIZE_SLOP, SignalingServer


def test_profiles_are_even_widths():
    """Odd dimensions are invalid for 4:2:0 and crash the encoder."""
    for name, (width, _bitrate) in PROFILES.items():
        assert width % 2 == 0, f"{name} width must be even"


def test_expected_profiles_exist():
    assert set(PROFILES) == {"text", "gui"}
    assert PROFILES["text"][0] < PROFILES["gui"][0], "text mode must be narrower"
    assert PROFILES["text"][1] < PROFILES["gui"][1], "text mode must be cheaper"


async def test_reconfigure_changes_frame_size():
    source = SwitchableSource(640, 400, 30)
    await source.start()
    try:
        first = await asyncio.wait_for(source.read(), timeout=15)
        assert (first.width, first.height) == (640, 400)

        await source.reconfigure(480, 300)
        assert (source.width, source.height) == (480, 300)

        second = await asyncio.wait_for(source.read(), timeout=15)
        assert (second.width, second.height) == (480, 300)
    finally:
        await source.stop()


async def test_reconfigure_to_the_same_size_is_a_no_op():
    source = SwitchableSource(640, 400, 30)
    await source.start()
    try:
        inner = source._inner
        await source.reconfigure(640, 400)
        assert source._inner is inner, "must not restart capture needlessly"
    finally:
        await source.stop()


async def test_read_survives_a_reconfigure_in_flight():
    """A reader parked on the old source must not hang forever."""
    source = SwitchableSource(640, 400, 30)
    await source.start()
    try:
        reader = asyncio.create_task(source.read())
        await asyncio.sleep(0.1)
        await source.reconfigure(480, 300)
        frame = await asyncio.wait_for(reader, timeout=15)
        assert frame.width in (640, 480)
    finally:
        await source.stop()


def _aligned(width: int) -> int:
    """What target_size will actually produce for a given cap."""
    return width - width % 16


def _server(source, max_width=1440):
    return SignalingServer(
        auth=SessionAuth(pin="424242"),
        source=source,
        controller=InputController(1470, 956, enabled=False),
        ice_servers=[],
        max_width=max_width,
    )


async def test_profile_message_resizes_capture():
    source = SwitchableSource(1440, 936, 30)
    await source.start()
    server = _server(source)
    try:
        server._handle_input(json.dumps({"t": "q", "p": "text"}))
        for _ in range(60):
            if source.width != 1440:
                break
            await asyncio.sleep(0.1)
        assert source.width == _aligned(PROFILES["text"][0])
    finally:
        await source.stop()


async def test_profile_switch_respects_max_width():
    """Otherwise switching profile silently undoes the user's cap."""
    source = SwitchableSource(800, 520, 30)
    await source.start()
    server = _server(source, max_width=900)
    try:
        assert server.apply_profile("gui") is True
        for _ in range(60):
            if source.width != 800:
                break
            await asyncio.sleep(0.1)
        assert source.width <= 900
    finally:
        await source.stop()


async def test_unknown_profile_is_ignored():
    source = SwitchableSource(640, 400, 30)
    await source.start()
    server = _server(source)
    try:
        assert server.apply_profile("nonsense") is False
        assert source.width == 640
    finally:
        await source.stop()


async def test_malformed_profile_message_does_not_raise():
    source = SwitchableSource(640, 400, 30)
    await source.start()
    server = _server(source)
    try:
        server._handle_input(json.dumps({"t": "q"}))
        server._handle_input("not json at all")
    finally:
        await source.stop()


async def test_read_does_not_raise_while_a_swap_is_in_flight():
    """reconfigure() nulls the inner source while rebuilding it."""
    source = SwitchableSource(640, 400, 30)
    await source.start()
    errors: list[BaseException] = []

    async def reader():
        try:
            for _ in range(30):
                await source.read()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            errors.append(exc)

    task = asyncio.create_task(reader())
    try:
        await asyncio.sleep(0.05)
        await source.reconfigure(480, 300)
        await source.reconfigure(560, 364)
        await asyncio.sleep(0.5)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await source.stop()
    assert not errors, f"read() raised during a swap: {errors[0]!r}"


async def test_read_raises_once_stopped():
    """Still an error after a genuine stop - just not mid-swap."""
    source = SwitchableSource(640, 400, 30)
    await source.start()
    await source.stop()
    with pytest.raises(RuntimeError, match="not running"):
        await asyncio.wait_for(source.read(), timeout=5)


async def test_viewport_narrows_the_capture():
    source = SwitchableSource(1440, 936, 30)
    await source.start()
    server = _server(source)
    try:
        server._handle_input(json.dumps({"t": "v", "w": 1170, "h": 2532}))
        for _ in range(60):
            if source.width != 1440:
                break
            await asyncio.sleep(0.1)
        assert source.width == _aligned(1170)
    finally:
        await source.stop()


async def test_viewport_never_widens_past_the_profile():
    """An iPad Pro reporting 2048 must not out-vote `text` mode."""
    source = SwitchableSource(1440, 936, 30)
    await source.start()
    server = _server(source)
    try:
        server.state.profile = "text"
        assert server._apply_viewport(2048, 1536) is True
        assert server._capture_width() == PROFILES["text"][0]
    finally:
        await source.stop()


async def test_viewport_never_widens_past_max_width():
    source = SwitchableSource(800, 520, 30)
    await source.start()
    server = _server(source, max_width=900)
    try:
        server._apply_viewport(2048, 1536)
        assert server._capture_width() <= 900
    finally:
        await source.stop()


async def test_tiny_viewport_is_floored():
    """A watch-sized report must not render the terminal unreadable."""
    source = SwitchableSource(1440, 936, 30)
    await source.start()
    server = _server(source)
    try:
        server._apply_viewport(120, 200)
        assert server._capture_width() == MIN_CAPTURE_WIDTH
    finally:
        await source.stop()


async def test_small_viewport_changes_do_not_restart_capture():
    """An address bar collapsing must not cost a keyframe and a hitch."""
    source = SwitchableSource(1440, 936, 30)
    await source.start()
    server = _server(source)
    try:
        assert server._apply_viewport(1170, 2532) is True
        inner = source._inner
        await asyncio.sleep(0.4)
        inner = source._inner
        assert server._apply_viewport(1170 + VIEWPORT_RESIZE_SLOP - 1, 2400) is False
        assert source._inner is inner
    finally:
        await source.stop()


async def test_implausible_viewport_is_rejected():
    source = SwitchableSource(640, 400, 30)
    await source.start()
    server = _server(source)
    try:
        for width, height in ((0, 0), (-10, 100), (99999, 100), (100, 99999)):
            assert server._apply_viewport(width, height) is False
        assert server.state.viewport_width is None
    finally:
        await source.stop()


async def test_malformed_viewport_message_does_not_raise():
    source = SwitchableSource(640, 400, 30)
    await source.start()
    server = _server(source)
    try:
        server._handle_input(json.dumps({"t": "v"}))
        server._handle_input(json.dumps({"t": "v", "w": "wide", "h": "tall"}))
        assert server.state.viewport_width is None
    finally:
        await source.stop()


async def test_profile_switch_keeps_the_viewport_cap():
    """Switching to gui on a phone must not jump back to the full 1440."""
    source = SwitchableSource(1440, 936, 30)
    await source.start()
    server = _server(source)
    try:
        server._apply_viewport(1170, 2532)
        server.apply_profile("text")
        assert server._capture_width() == PROFILES["text"][0]
        server.apply_profile("gui")
        assert server._capture_width() == 1170
    finally:
        await source.stop()


async def test_starting_profile_is_honoured():
    """Otherwise the first viewport message resizes to a profile nobody chose."""
    source = SwitchableSource(1100, 715, 30)
    await source.start()
    server = SignalingServer(
        auth=SessionAuth(pin="424242"),
        source=source,
        controller=InputController(1470, 956, enabled=False),
        ice_servers=[],
        profile="text",
    )
    try:
        assert server.state.profile == "text"
        server._apply_viewport(2048, 1536)
        assert server._capture_width() == PROFILES["text"][0]
    finally:
        await source.stop()
