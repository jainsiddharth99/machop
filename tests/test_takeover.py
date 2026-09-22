"""Closing one browser and opening another must not lock you out."""

import asyncio
import json

import aiohttp
import pytest

pytest.importorskip("Quartz", reason="macOS only")

from aiortc import RTCPeerConnection

from machop.capture import SwitchableSource
from machop.inputs import InputController
from machop.security import SessionAuth
from machop.server import SignalingServer, start_server

PORT = 18995
PIN = "424242"


async def _offer_sdp():
    pc = RTCPeerConnection()
    pc.addTransceiver("video", direction="recvonly")
    pc.createDataChannel("input")
    await pc.setLocalDescription(await pc.createOffer())
    return pc, pc.localDescription.sdp


async def _post(http, sdp, pin, cookies=None):
    async with http.post(
        f"http://127.0.0.1:{PORT}/offer",
        json={"pin": pin, "sdp": sdp, "type": "offer"},
        cookies=cookies or {},
    ) as response:
        return response.status, await response.text(), response.cookies


class _Fixture:
    def __init__(self):
        self.source = None
        self.server = None
        self.runner = None
        self.peers = []

    async def start(self):
        self.source = SwitchableSource(640, 400, 30)
        await self.source.start()
        self.server = SignalingServer(
            auth=SessionAuth(pin=PIN),
            source=self.source,
            controller=InputController(1470, 956, enabled=False),
            ice_servers=[],
        )
        self.runner = await start_server(self.server, port=PORT, host="127.0.0.1")
        return self

    async def stop(self):
        for pc in self.peers:
            await pc.close()
        await self.server.close()
        await self.runner.cleanup()
        await self.source.stop()


@pytest.fixture
async def rig():
    fixture = await _Fixture().start()
    try:
        yield fixture
    finally:
        await fixture.stop()


async def test_a_second_browser_with_the_code_gets_in(rig):
    """The bug: this used to be 409 until ICE timed out, minutes later."""
    async with aiohttp.ClientSession() as http:
        pc_a, sdp_a = await _offer_sdp()
        rig.peers.append(pc_a)
        status, _body, _c = await _post(http, sdp_a, PIN)
        assert status == 200

    async with aiohttp.ClientSession() as http:
        pc_b, sdp_b = await _offer_sdp()
        rig.peers.append(pc_b)
        status, body, _c = await _post(http, sdp_b, PIN)
        assert status == 200, f"second browser was refused: {body}"


async def test_the_displaced_viewer_is_hung_up_on(rig):
    """Leaving it connected would mean two viewers on one session, which is exactly what the 409 was there to prevent."""
    async with aiohttp.ClientSession() as http:
        pc_a, sdp_a = await _offer_sdp()
        rig.peers.append(pc_a)
        await _post(http, sdp_a, PIN)
        assert len(rig.server.state.peers) == 1

    async with aiohttp.ClientSession() as http:
        pc_b, sdp_b = await _offer_sdp()
        rig.peers.append(pc_b)
        await _post(http, sdp_b, PIN)

    assert len(rig.server.state.peers) == 1, "the old peer should be gone"


async def test_the_takeover_keeps_the_claim(rig):
    """Evicting the old viewer runs the same teardown as a disconnect."""
    async with aiohttp.ClientSession() as http:
        pc_a, sdp_a = await _offer_sdp()
        rig.peers.append(pc_a)
        await _post(http, sdp_a, PIN)

    async with aiohttp.ClientSession() as http:
        pc_b, sdp_b = await _offer_sdp()
        rig.peers.append(pc_b)
        await _post(http, sdp_b, PIN)

    await asyncio.sleep(0.2)
    assert rig.server.state.auth.claimed, "claim was released by the eviction"


async def test_a_wrong_code_evicts_nobody(rig):
    async with aiohttp.ClientSession() as http:
        pc_a, sdp_a = await _offer_sdp()
        rig.peers.append(pc_a)
        await _post(http, sdp_a, PIN)

    async with aiohttp.ClientSession() as http:
        pc_b, sdp_b = await _offer_sdp()
        rig.peers.append(pc_b)
        status, _body, _c = await _post(http, sdp_b, "000000")
        assert status == 401

    assert len(rig.server.state.peers) == 1, "the real viewer was dropped"


async def test_the_same_browser_reconnecting_is_not_a_takeover(rig):
    """A reconnect must not tear down a connection that is still coming up."""
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as http:
        pc_a, sdp_a = await _offer_sdp()
        rig.peers.append(pc_a)
        status, _b, _c = await _post(http, sdp_a, PIN)
        assert status == 200

        pc_b, sdp_b = await _offer_sdp()
        rig.peers.append(pc_b)
        status, _b, _c = await _post(http, sdp_b, PIN)
        assert status == 200
        assert rig.server.state.auth.claimed
