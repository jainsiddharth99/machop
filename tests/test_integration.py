"""End-to-end negotiation against the real server."""

import asyncio

import pytest

pytest.importorskip("Quartz", reason="macOS only")

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

from machop.capture import create_frame_source, display_geometry, target_size
from machop.inputs import InputController
from machop.security import SessionAuth
from machop.server import SignalingServer, start_server

PIN = "424242"


@pytest.fixture
async def running_server(unused_tcp_port):
    geometry = display_geometry()
    size = target_size(geometry, 960)
    source = await create_frame_source(size[0], size[1], 30)
    controller = InputController(geometry.width, geometry.height, enabled=False)
    server = SignalingServer(
        auth=SessionAuth(pin=PIN),
        source=source,
        controller=controller,
        ice_servers=[],
    )
    runner = await start_server(server, port=unused_tcp_port)
    try:
        yield server, unused_tcp_port
    finally:
        await server.close()
        await runner.cleanup()
        await source.stop()


def make_viewer():
    """A peer shaped exactly like the browser client in web/app.js."""
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    pc.createDataChannel("input", ordered=True)
    pc.addTransceiver("video", direction="recvonly")
    return pc


async def post_offer(client, port, pc, pin=PIN):
    await pc.setLocalDescription(await pc.createOffer())
    return await client.post(
        f"http://127.0.0.1:{port}/offer",
        json={"pin": pin, "sdp": pc.localDescription.sdp, "type": "offer"},
    )


async def test_offer_is_accepted_and_answers_with_video(running_server, aiohttp_client_session=None):
    import aiohttp

    server, port = running_server
    pc = make_viewer()
    try:
        async with aiohttp.ClientSession() as client:
            response = await post_offer(client, port, pc)
            assert response.status == 200
            answer = await response.json()
    finally:
        await pc.close()

    assert "m=video" in answer["sdp"], "answer must negotiate a video m-line"


async def test_negotiated_codec_is_h264_not_vp8(running_server):
    """VP8 at screen resolution measures 2405 ms/frame. It must never win."""
    import aiohttp

    server, port = running_server
    pc = make_viewer()
    try:
        async with aiohttp.ClientSession() as client:
            answer = await (await post_offer(client, port, pc)).json()
    finally:
        await pc.close()

    rtpmaps = [l for l in answer["sdp"].splitlines() if l.startswith("a=rtpmap")]
    video_maps = [l for l in rtpmaps if "/90000" in l and "rtx" not in l.lower()]
    assert video_maps, "no video codec in answer"
    assert "H264" in video_maps[0], f"expected H264 first, got {video_maps[0]}"
    assert not any("VP8" in l for l in video_maps), "VP8 must not be offered back"


async def test_video_frames_actually_arrive(running_server):
    import aiohttp

    server, port = running_server
    pc = make_viewer()
    received: list = []
    done = asyncio.Event()

    @pc.on("track")
    def on_track(track):
        async def pump():
            while len(received) < 3:
                received.append(await track.recv())
            done.set()

        asyncio.ensure_future(pump())

    try:
        async with aiohttp.ClientSession() as client:
            answer = await (await post_offer(client, port, pc)).json()
        await pc.setRemoteDescription(RTCSessionDescription(**answer))
        await asyncio.wait_for(done.wait(), timeout=25)
    finally:
        await pc.close()

    assert len(received) >= 3
    assert received[0].width > 0 and received[0].height > 0


async def test_wrong_pin_is_rejected(running_server):
    import aiohttp

    server, port = running_server
    pc = make_viewer()
    try:
        async with aiohttp.ClientSession() as client:
            response = await post_offer(client, port, pc, pin="000000")
            assert response.status == 401
    finally:
        await pc.close()


async def test_offer_without_video_is_a_client_error_not_a_500(running_server):
    """v0.1 raised ValueError inside aiortc and returned HTTP 500 here."""
    import aiohttp

    server, port = running_server
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    pc.createDataChannel("input")
    try:
        async with aiohttp.ClientSession() as client:
            response = await post_offer(client, port, pc)
            assert response.status == 400, f"expected 400, got {response.status}"
    finally:
        await pc.close()


async def test_malformed_body_is_rejected(running_server):
    import aiohttp

    server, port = running_server
    async with aiohttp.ClientSession() as client:
        response = await client.post(
            f"http://127.0.0.1:{port}/offer", data="not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status == 400


async def test_index_and_assets_are_served(running_server):
    import aiohttp

    server, port = running_server
    async with aiohttp.ClientSession() as client:
        page = await client.get(f"http://127.0.0.1:{port}/")
        assert page.status == 200
        body = await page.text()
        assert "Machop" in body

        script = await client.get(f"http://127.0.0.1:{port}/static/app.js")
        assert script.status == 200
        source = await script.text()
        assert "addTransceiver('video', { direction: 'recvonly' })" in source


def _lan_address() -> str | None:
    """This host's routable address, or None when offline."""
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith("127.") else address


async def test_server_binds_loopback_only(running_server):
    """v0.1 bound 0.0.0.0, exposing screen control to the whole LAN."""
    import socket

    server, port = running_server
    address = _lan_address()
    if address is None:
        pytest.skip("no non-loopback address available")

    probe = socket.socket()
    probe.settimeout(1.0)
    try:
        result = probe.connect_ex((address, port))
    finally:
        probe.close()
    assert result != 0, f"port {port} is reachable on {address}; must be loopback only"


async def test_malformed_offer_does_not_claim_the_session(running_server):
    """A bad offer bearing the correct code must not lock out the real viewer."""
    import aiohttp

    server, port = running_server
    async with aiohttp.ClientSession() as client:
        bad = await client.post(
            f"http://127.0.0.1:{port}/offer",
            json={"pin": PIN, "sdp": "v=0", "type": "offer"},
        )
        assert bad.status == 400

    pc = make_viewer()
    try:
        async with aiohttp.ClientSession() as client:
            response = await post_offer(client, port, pc)
            assert response.status == 200, (
                f"a legitimate viewer got {response.status} after a malformed "
                f"offer; the session was left claimed"
            )
    finally:
        await pc.close()


async def test_wrong_pin_then_right_pin_still_connects(running_server):
    """A typo must not consume the session either."""
    import aiohttp

    server, port = running_server
    pc_bad = make_viewer()
    pc_good = make_viewer()
    try:
        async with aiohttp.ClientSession() as client:
            rejected = await post_offer(client, port, pc_bad, pin="000000")
            assert rejected.status == 401
            accepted = await post_offer(client, port, pc_good)
            assert accepted.status == 200
    finally:
        await pc_bad.close()
        await pc_good.close()
