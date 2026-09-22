"""Power assertions, verified against pmset rather than against a mock."""

import subprocess
import time

import pytest

from machop.power import PowerAssertions, on_battery, startup_warnings


def _pmset_assertions() -> str:
    return subprocess.run(
        ["pmset", "-g", "assertions"], capture_output=True, text=True
    ).stdout


def _wait_until_absent(needle: str = "machop", timeout: float = 3.0) -> bool:
    """pmset reflects a release a moment after the call returns, so polling here is honest rather than flaky."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle not in _pmset_assertions():
            return True
        time.sleep(0.1)
    return False


def test_system_assertion_is_visible_to_the_os():
    power = PowerAssertions()
    power.hold_system()
    try:
        assert "machop" in _pmset_assertions()
        assert power.held == frozenset({"system"})
    finally:
        power.release_all()


def test_releasing_removes_it_from_the_os():
    power = PowerAssertions()
    power.hold_system()
    power.release_system()
    assert _wait_until_absent()
    assert power.held == frozenset()


def test_display_assertion_is_independent_of_system():
    power = PowerAssertions()
    power.hold_system()
    power.hold_display()
    try:
        assert power.held == frozenset({"system", "display"})
        power.release_display()
        assert power.held == frozenset({"system"})
        assert "machop" in _pmset_assertions(), "system assertion must survive"
    finally:
        power.release_all()


def test_holding_twice_is_idempotent():
    """Two viewers connecting must not create two assertions, or the second disconnect leaves one behind."""
    power = PowerAssertions()
    power.hold_display()
    first = power._ids.get("display")
    power.hold_display()
    try:
        assert power._ids.get("display") == first
    finally:
        power.release_all()


def test_releasing_when_not_held_is_safe():
    power = PowerAssertions()
    power.release_display()
    power.release_system()
    power.release_all()


def test_context_manager_releases_on_exception():
    with pytest.raises(RuntimeError):
        with PowerAssertions() as power:
            power.hold_system()
            power.hold_display()
            raise RuntimeError("boom")
    assert _wait_until_absent()


def test_wake_display_reports_success():
    with PowerAssertions() as power:
        assert power.wake_display() is True


def test_on_battery_returns_a_bool():
    assert isinstance(on_battery(), bool)


def test_battery_produces_the_clamshell_warning():
    warnings = startup_warnings()
    assert isinstance(warnings, list)
    assert any("lit" in w for w in warnings)
    if on_battery():
        assert any("lid" in w.lower() for w in warnings), (
            "closing the lid on battery sleeps regardless of any assertion; "
            "the user must be told"
        )


import asyncio

pytest.importorskip("Quartz", reason="macOS only")

from machop.capture import create_frame_source, display_geometry, target_size
from machop.inputs import InputController
from machop.security import SessionAuth
from machop.server import SignalingServer, start_server


async def test_display_assertion_tracks_viewer_presence(unused_tcp_port):
    """Held while someone is watching, released when nobody is."""
    import aiohttp
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

    events: list[str] = []
    geometry = display_geometry()
    size = target_size(geometry, 640)
    source = await create_frame_source(size[0], size[1], 30)
    server = SignalingServer(
        auth=SessionAuth(pin="424242"),
        source=source,
        controller=InputController(geometry.width, geometry.height, enabled=False),
        ice_servers=[],
        on_viewer_active=lambda: events.append("active"),
        on_viewer_idle=lambda: events.append("idle"),
    )
    runner = await start_server(server, port=unused_tcp_port)

    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    pc.createDataChannel("input")
    pc.addTransceiver("video", direction="recvonly")
    try:
        await pc.setLocalDescription(await pc.createOffer())
        async with aiohttp.ClientSession() as http:
            response = await http.post(
                f"http://127.0.0.1:{unused_tcp_port}/offer",
                json={
                    "pin": "424242",
                    "sdp": pc.localDescription.sdp,
                    "type": "offer",
                },
            )
            answer = await response.json()
        await pc.setRemoteDescription(RTCSessionDescription(**answer))
        assert server.viewer_count == 1
        assert events == ["active"]
    finally:
        await pc.close()
        await server.close()
        await runner.cleanup()
        await source.stop()

    assert events == ["active", "idle"], "idle must fire once the viewer is gone"


def test_releasing_the_display_also_drops_the_wake_assertion():
    """UserIsActive persists until released - it does not time out."""
    power = PowerAssertions()
    power.hold_system()
    power.hold_display()
    power.wake_display()
    try:
        assert power._wake_id.value != 0
        power.release_display()
        assert power._wake_id.value == 0
        assert _wait_until_absent("machop wake")
        assert "machop (system)" in _pmset_assertions(), "system must survive"
    finally:
        power.release_all()


def test_wake_display_does_not_leak_an_assertion():
    """IOPMAssertionDeclareUserActivity returns an id; passing a fresh zero on every call creates a new assertion each time and never frees it, so each viewer reconnect would leak one."""
    power = PowerAssertions()
    try:
        for _ in range(5):
            assert power.wake_display() is True
        first = power._wake_id.value
        power.wake_display()
        assert power._wake_id.value == first, "a new assertion was created"
    finally:
        power.release_all()
    assert power._wake_id.value == 0


def test_release_all_clears_the_wake_assertion():
    power = PowerAssertions()
    power.hold_system()
    power.wake_display()
    power.release_all()
    assert _wait_until_absent()
