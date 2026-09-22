"""The encrypted relay fallback: crypto, wire format, and a live session."""

import asyncio
import base64
import json

import av

import pytest

from machop.relay import (
    TAG_CONFIG,
    TAG_INPUT,
    TAG_READY,
    TAG_VIDEO,
    RelayProtocolError,
    RelaySession,
)


def paired(pin_server="424242", pin_client="424242"):
    server = RelaySession(pin_server, role="server")
    client = RelaySession(pin_client, role="client", salt=server.salt)
    server_tag = server.establish(client.public_key_bytes)
    client_tag = client.establish(server.public_key_bytes)
    return server, client, server_tag, client_tag


def test_confirmation_tags_agree():
    _, _, server_tag, client_tag = paired()
    assert server_tag == client_tag


def test_records_travel_both_ways():
    server, client, _, _ = paired()
    assert client.open(server.seal(TAG_VIDEO, b"frame")) == (TAG_VIDEO, b"frame")
    assert server.open(client.seal(TAG_INPUT, b"{}")) == (TAG_INPUT, b"{}")


def test_wrong_pin_yields_different_tags_and_cannot_decrypt():
    server, client, server_tag, client_tag = paired(pin_client="999999")
    assert server_tag != client_tag
    with pytest.raises(Exception):
        client.open(server.seal(TAG_VIDEO, b"secret"))


def test_nonces_never_repeat():
    server, _, _, _ = paired()
    nonces = {server._send.next_nonce() for _ in range(2000)}
    assert len(nonces) == 2000


def test_replayed_record_is_rejected():
    server, client, _, _ = paired()
    record = server.seal(TAG_VIDEO, b"once")
    client.open(record)
    with pytest.raises(RelayProtocolError, match="Replay"):
        client.open(record)


def test_tampered_ciphertext_is_rejected():
    server, client, _, _ = paired()
    record = bytearray(server.seal(TAG_VIDEO, b"payload"))
    record[-1] ^= 0xFF
    with pytest.raises(Exception):
        client.open(bytes(record))


def test_truncated_record_is_rejected():
    server, client, _, _ = paired()
    with pytest.raises(RelayProtocolError):
        client.open(b"\x00\x01")


def test_malformed_peer_key_is_rejected():
    server = RelaySession("424242", role="server")
    with pytest.raises(RelayProtocolError):
        server.establish(b"not a point")


pytest.importorskip("Quartz", reason="macOS only")

from machop.capture import create_frame_source, display_geometry, target_size
from machop.inputs import InputController
from machop.security import SessionAuth
from machop.server import SignalingServer, start_server

PIN = "424242"


@pytest.fixture
async def relay_server(unused_tcp_port):
    geometry = display_geometry()
    size = target_size(geometry, 640)
    source = await create_frame_source(size[0], size[1], 30)
    controller = InputController(geometry.width, geometry.height, enabled=False)
    server = SignalingServer(
        auth=SessionAuth(pin=PIN), source=source, controller=controller, ice_servers=[]
    )
    runner = await start_server(server, port=unused_tcp_port)
    try:
        yield server, unused_tcp_port
    finally:
        await server.close()
        await runner.cleanup()
        await source.stop()


async def test_relay_streams_encrypted_video(relay_server):
    import aiohttp

    _, port = relay_server
    client = RelaySession(PIN, role="client")

    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(f"http://127.0.0.1:{port}/relay") as ws:
            await ws.send_json({
                "pin": PIN,
                "pub": base64.b64encode(client.public_key_bytes).decode(),
            })
            hello = await asyncio.wait_for(ws.receive_json(), timeout=10)

            client.salt = base64.b64decode(hello["salt"])
            tag = client.establish(base64.b64decode(hello["pub"]))
            assert tag == base64.b64decode(hello["confirm"]), "server failed confirmation"

            await ws.send_bytes(client.seal(TAG_READY, b""))

            tags, video_bytes = [], 0
            deadline = asyncio.get_running_loop().time() + 20
            while len(tags) < 3 and asyncio.get_running_loop().time() < deadline:
                message = await asyncio.wait_for(ws.receive(), timeout=15)
                if message.type is not aiohttp.WSMsgType.BINARY:
                    continue
                record_tag, payload = client.open(message.data)
                tags.append(record_tag)
                if record_tag == TAG_VIDEO:
                    video_bytes += len(payload)

    assert tags[0] == TAG_CONFIG, "first record must configure the decoder"
    assert TAG_VIDEO in tags, "no video records received"
    assert video_bytes > 0


async def test_relay_rejects_a_wrong_pin(relay_server):
    import aiohttp

    _, port = relay_server
    client = RelaySession("000000", role="client")
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(f"http://127.0.0.1:{port}/relay") as ws:
            await ws.send_json({
                "pin": "000000",
                "pub": base64.b64encode(client.public_key_bytes).decode(),
            })
            message = await asyncio.wait_for(ws.receive(), timeout=10)
            assert message.type is aiohttp.WSMsgType.CLOSE
            assert ws.close_code == 4401


async def test_relay_fires_the_bandwidth_warning_once():
    """The relay is the one path that spends tunnel bandwidth, so engaging it has to be visible - a metered backend would otherwise just die."""
    import aiohttp

    from machop.capture import SwitchableSource
    from machop.inputs import InputController
    from machop.security import SessionAuth
    from machop.server import SignalingServer, start_server

    calls = []
    source = SwitchableSource(640, 400, 30)
    await source.start()
    server = SignalingServer(
        auth=SessionAuth(pin="424242"),
        source=source,
        controller=InputController(1470, 956, enabled=False),
        ice_servers=[],
        on_relay_active=lambda forced=False: calls.append(forced),
    )
    runner = await start_server(server, port=18993, host="127.0.0.1")
    try:
        client = RelaySession("424242", role="client")
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect("http://127.0.0.1:18993/relay") as ws:
                await ws.send_str(json.dumps({
                    "pin": "424242",
                    "pub": base64.b64encode(client.public_key_bytes).decode(),
                    "w": 4096, "h": 4096,
                }))
                hello = json.loads(await ws.receive_str())
                assert "confirm" in hello
                await asyncio.sleep(0.2)
        assert calls == [False], (
            f"expected one warning, marked as a fallback not a request, "
            f"got {calls}"
        )
    finally:
        await server.close()
        await runner.cleanup()
        await source.stop()


async def test_the_relay_survives_the_capture_resizing_under_it():
    """The grey-screen bug, reproduced."""
    import aiohttp

    from machop.capture import SwitchableSource
    from machop.inputs import InputController
    from machop.security import SessionAuth
    from machop.server import SignalingServer, start_server

    source = SwitchableSource(960, 624, 30)
    await source.start()
    server = SignalingServer(
        auth=SessionAuth(pin="424242"),
        source=source,
        controller=InputController(1470, 956, enabled=False),
        ice_servers=[],
    )
    runner = await start_server(server, port=18996, host="127.0.0.1")
    try:
        client = RelaySession("424242", role="client")
        configs: list[dict] = []
        video_after_resize = 0
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect("http://127.0.0.1:18996/relay") as ws:
                await ws.send_str(json.dumps({
                    "pin": "424242",
                    "pub": base64.b64encode(client.public_key_bytes).decode(),
                }))
                hello = json.loads(await ws.receive_str())
                client.salt = base64.b64decode(hello["salt"])
                tag = client.establish(base64.b64decode(hello["pub"]))
                assert tag == base64.b64decode(hello["confirm"])
                await ws.send_bytes(client.seal(TAG_READY, b""))

                async def drain(seconds):
                    nonlocal video_after_resize
                    deadline = asyncio.get_running_loop().time() + seconds
                    while asyncio.get_running_loop().time() < deadline:
                        try:
                            message = await asyncio.wait_for(ws.receive(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        if message.type is not aiohttp.WSMsgType.BINARY:
                            continue
                        tag, payload = client.open(message.data)
                        if tag == TAG_CONFIG:
                            configs.append(json.loads(payload.decode()))
                        elif tag == TAG_VIDEO and len(configs) > 1:
                            video_after_resize += 1

                await drain(2.0)
                assert configs, "no stream config was ever sent"
                assert (configs[0]["width"], configs[0]["height"]) == (960, 624)

                await source.reconfigure(720, 468)
                await drain(4.0)

        assert len(configs) >= 2, (
            f"the resize was never announced; viewer would decode 720x468 "
            f"into a {configs[0]['width']}x{configs[0]['height']} canvas"
        )
        assert (configs[-1]["width"], configs[-1]["height"]) == (720, 468)
        assert video_after_resize > 0, "the pump died instead of resizing"
    finally:
        await server.close()
        await runner.cleanup()
        await source.stop()


async def test_a_requested_relay_is_not_reported_as_a_failure():
    """`?relay=1` means the viewer never tried peer-to-peer, so saying it failed is simply untrue - and it is the line that tells you whether something is wrong with your network."""
    import aiohttp

    from machop.capture import SwitchableSource
    from machop.inputs import InputController
    from machop.security import SessionAuth
    from machop.server import SignalingServer, start_server

    calls: list[bool] = []
    source = SwitchableSource(640, 400, 30)
    await source.start()
    server = SignalingServer(
        auth=SessionAuth(pin="424242"),
        source=source,
        controller=InputController(1470, 956, enabled=False),
        ice_servers=[],
        on_relay_active=lambda forced=False: calls.append(forced),
    )
    runner = await start_server(server, port=18998, host="127.0.0.1")
    try:
        client = RelaySession("424242", role="client")
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect("http://127.0.0.1:18998/relay") as ws:
                await ws.send_str(json.dumps({
                    "pin": "424242",
                    "pub": base64.b64encode(client.public_key_bytes).decode(),
                    "forced": True,
                }))
                await ws.receive_str()
                await asyncio.sleep(0.2)
        assert calls == [True], f"relay was reported as a fallback: {calls}"
    finally:
        await server.close()
        await runner.cleanup()
        await source.stop()


async def test_a_keyframe_request_is_answered_on_a_still_screen():
    """The recovery path only works if it can be answered."""
    import aiohttp

    from machop.capture import FrameSource
    from machop.inputs import InputController
    from machop.security import SessionAuth
    from machop.server import SignalingServer, start_server

    class StillScreen(FrameSource):
        """Delivers one frame, then nothing - a Mac nobody is touching."""

        def __init__(self):
            super().__init__(320, 240)
            self.sent = False

        async def start(self): ...

        async def stop(self): ...

        async def read(self):
            if self.sent:
                await asyncio.sleep(3600)
            self.sent = True
            frame = av.VideoFrame(320, 240, "yuv420p")
            return frame

    source = StillScreen()
    server = SignalingServer(
        auth=SessionAuth(pin="424242"),
        source=source,
        controller=InputController(1470, 956, enabled=False),
        ice_servers=[],
    )
    runner = await start_server(server, port=18999, host="127.0.0.1")
    try:
        client = RelaySession("424242", role="client")
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect("http://127.0.0.1:18999/relay") as ws:
                await ws.send_str(json.dumps({
                    "pin": "424242",
                    "pub": base64.b64encode(client.public_key_bytes).decode(),
                    "w": 4096, "h": 4096,
                }))
                hello = json.loads(await ws.receive_str())
                client.salt = base64.b64decode(hello["salt"])
                client.establish(base64.b64decode(hello["pub"]))
                await ws.send_bytes(client.seal(TAG_READY, b""))

                async def collect(seconds):
                    keys = 0
                    deadline = asyncio.get_running_loop().time() + seconds
                    while asyncio.get_running_loop().time() < deadline:
                        try:
                            message = await asyncio.wait_for(ws.receive(), timeout=0.5)
                        except asyncio.TimeoutError:
                            continue
                        if message.type is not aiohttp.WSMsgType.BINARY:
                            continue
                        tag, payload = client.open(message.data)
                        if tag == TAG_VIDEO and payload[0] == 1:
                            keys += 1
                    return keys

                await collect(2.5)
                quiet = await collect(1.5)
                assert quiet == 0, f"a still screen should go quiet, got {quiet}"

                await ws.send_bytes(client.seal(
                    TAG_INPUT, json.dumps({"t": "kf"}).encode()))
                assert await collect(3.0) > 0, (
                    "no keyframe arrived, so a recovering viewer stays grey"
                )
    finally:
        await server.close()
        await runner.cleanup()


async def test_a_hidden_viewer_costs_nothing():
    """A phone in a pocket has nothing to show it."""
    import aiohttp

    from machop.capture import SwitchableSource
    from machop.inputs import InputController
    from machop.security import SessionAuth
    from machop.server import SignalingServer, start_server

    source = SwitchableSource(640, 400, 30)
    await source.start()
    server = SignalingServer(
        auth=SessionAuth(pin="424242"),
        source=source,
        controller=InputController(1470, 956, enabled=False),
        ice_servers=[],
    )
    runner = await start_server(server, port=19001, host="127.0.0.1")
    try:
        client = RelaySession("424242", role="client")
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect("http://127.0.0.1:19001/relay") as ws:
                await ws.send_str(json.dumps({
                    "pin": "424242",
                    "pub": base64.b64encode(client.public_key_bytes).decode(),
                    "w": 4096, "h": 4096,
                }))
                hello = json.loads(await ws.receive_str())
                client.salt = base64.b64decode(hello["salt"])
                client.establish(base64.b64decode(hello["pub"]))
                await ws.send_bytes(client.seal(TAG_READY, b""))

                async def bytes_over(seconds):
                    total = 0
                    deadline = asyncio.get_running_loop().time() + seconds
                    while asyncio.get_running_loop().time() < deadline:
                        try:
                            message = await asyncio.wait_for(ws.receive(), timeout=0.4)
                        except asyncio.TimeoutError:
                            continue
                        if message.type is not aiohttp.WSMsgType.BINARY:
                            continue
                        tag, payload = client.open(message.data)
                        if tag == TAG_VIDEO:
                            total += len(payload)
                    return total

                await bytes_over(2.0)
                await ws.send_bytes(client.seal(
                    TAG_INPUT, json.dumps({"t": "pv", "on": False}).encode()))
                await asyncio.sleep(0.5)
                assert await bytes_over(2.0) == 0, "video kept flowing to a hidden tab"

                await ws.send_bytes(client.seal(
                    TAG_INPUT, json.dumps({"t": "pv", "on": True}).encode()))
                assert await bytes_over(3.0) > 0, "video did not resume"
    finally:
        await server.close()
        await runner.cleanup()
        await source.stop()


async def test_a_viewer_that_drops_while_hidden_does_not_pause_the_next_one():
    """viewer_hidden is server state, so it outlives the viewer that set it."""
    import aiohttp

    from machop.capture import SwitchableSource
    from machop.inputs import InputController
    from machop.security import SessionAuth
    from machop.server import SignalingServer, start_server

    source = SwitchableSource(640, 400, 30)
    await source.start()
    server = SignalingServer(
        auth=SessionAuth(pin="424242"),
        source=source,
        controller=InputController(1470, 956, enabled=False),
        ice_servers=[],
    )
    runner = await start_server(server, port=19002, host="127.0.0.1")

    async def session(hide: bool) -> int:
        client = RelaySession("424242", role="client")
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect("http://127.0.0.1:19002/relay") as ws:
                await ws.send_str(json.dumps({
                    "pin": "424242",
                    "pub": base64.b64encode(client.public_key_bytes).decode(),
                    "w": 4096, "h": 4096,
                }))
                hello = json.loads(await ws.receive_str())
                client.salt = base64.b64decode(hello["salt"])
                client.establish(base64.b64decode(hello["pub"]))
                await ws.send_bytes(client.seal(TAG_READY, b""))
                if hide:
                    await ws.send_bytes(client.seal(
                        TAG_INPUT, json.dumps({"t": "pv", "on": False}).encode()))
                    await asyncio.sleep(0.6)
                    return 0
                total = 0
                deadline = asyncio.get_running_loop().time() + 3.0
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        message = await asyncio.wait_for(ws.receive(), timeout=0.4)
                    except asyncio.TimeoutError:
                        continue
                    if message.type is not aiohttp.WSMsgType.BINARY:
                        continue
                    tag, payload = client.open(message.data)
                    if tag == TAG_VIDEO:
                        total += len(payload)
                return total

    try:
        await session(hide=True)
        await asyncio.sleep(0.3)
        assert await session(hide=False) > 0, "the next viewer got a paused stream"
    finally:
        await server.close()
        await runner.cleanup()
        await source.stop()


def test_the_codec_string_is_read_from_the_stream():
    import av
    import numpy as np

    from machop.relay import AnnexBEncoder, codec_string_from_annexb

    enc = AnnexBEncoder(640, 400, 1_000_000)
    arr = np.random.default_rng(0).integers(0, 255, (400, 640, 3), dtype=np.uint8)
    packets = enc.encode(av.VideoFrame.from_ndarray(arr, format="rgb24"))
    assert packets, "no packet to read a codec string out of"
    from_stream = codec_string_from_annexb(packets[0][2])
    assert from_stream == enc.codec_string, "announced string is not the stream's"


def test_the_level_is_pinned_within_what_phones_decode():
    """Unconstrained, x264 labels this stream level 6.2, which no iPhone's hardware decoder accepts - frames arrive and nothing appears."""
    import av
    import numpy as np

    from machop.relay import AnnexBEncoder

    for width, height in ((640, 400), (1290, 838), (1440, 936)):
        enc = AnnexBEncoder(width, height, 2_000_000)
        arr = np.random.default_rng(0).integers(
            0, 255, (height, width, 3), dtype=np.uint8
        )
        enc.encode(av.VideoFrame.from_ndarray(arr, format="rgb24"))
        level = int(enc.codec_string[-2:], 16)
        assert level <= 0x2A, (
            f"{width}x{height} declared level {level / 10}; iOS hardware tops "
            f"out around 5.2 and 4.2 is the safe choice"
        )
        assert enc.codec_string.startswith("avc1.42"), "must stay Baseline"


def test_a_stream_with_no_sps_leaves_the_default_alone():
    from machop.relay import DEFAULT_CODEC_STRING, codec_string_from_annexb

    assert codec_string_from_annexb(b"") is None
    assert codec_string_from_annexb(b"\x00\x00\x01\x41\xff\xff") is None
    assert DEFAULT_CODEC_STRING == "avc1.42C02A"


def test_both_start_code_lengths_are_understood():
    from machop.relay import codec_string_from_annexb

    sps = bytes([0x67, 0x42, 0xC0, 0x2A, 0x00])
    assert codec_string_from_annexb(b"\x00\x00\x01" + sps) == "avc1.42C02A"
    assert codec_string_from_annexb(b"\x00\x00\x00\x01" + sps) == "avc1.42C02A"


async def test_the_announced_config_carries_the_real_codec_string():
    import aiohttp

    from machop.capture import SwitchableSource
    from machop.inputs import InputController
    from machop.security import SessionAuth
    from machop.server import SignalingServer, start_server

    source = SwitchableSource(640, 400, 30)
    await source.start()
    server = SignalingServer(
        auth=SessionAuth(pin="424242"),
        source=source,
        controller=InputController(1470, 956, enabled=False),
        ice_servers=[],
    )
    runner = await start_server(server, port=19003, host="127.0.0.1")
    try:
        client = RelaySession("424242", role="client")
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect("http://127.0.0.1:19003/relay") as ws:
                await ws.send_str(json.dumps({
                    "pin": "424242",
                    "pub": base64.b64encode(client.public_key_bytes).decode(),
                    "w": 4096, "h": 4096,
                }))
                hello = json.loads(await ws.receive_str())
                client.salt = base64.b64decode(hello["salt"])
                client.establish(base64.b64decode(hello["pub"]))
                await ws.send_bytes(client.seal(TAG_READY, b""))

                config = None
                deadline = asyncio.get_running_loop().time() + 15
                while config is None and asyncio.get_running_loop().time() < deadline:
                    message = await asyncio.wait_for(ws.receive(), timeout=10)
                    if message.type is not aiohttp.WSMsgType.BINARY:
                        continue
                    tag, payload = client.open(message.data)
                    if tag == TAG_CONFIG:
                        config = json.loads(payload.decode())
                assert config is not None, "no config was announced"
                assert config["codec"] != "avc1.42E01E", (
                    "the old hardcoded string is back; it claims level 3.0 for a "
                    "stream that is nothing of the sort"
                )
                assert int(config["codec"][-2:], 16) <= 0x2A
    finally:
        await server.close()
        await runner.cleanup()
        await source.stop()


def _slice_count(data: bytes) -> int:
    """Coded slices (NAL types 1 and 5) in an Annex-B buffer."""
    count, index = 0, 0
    while index < len(data) - 4:
        if data[index : index + 3] == b"\x00\x00\x01":
            start = index + 3
        elif data[index : index + 4] == b"\x00\x00\x00\x01":
            start = index + 4
        else:
            index += 1
            continue
        if (data[start] & 0x1F) in (1, 5):
            count += 1
        index = start + 1
    return count


def test_every_frame_is_a_single_slice():
    """The grey screen with a band of picture across the top."""
    import av
    import numpy as np

    from machop.relay import AnnexBEncoder

    for width, height in ((640, 400), (1290, 838), (1440, 936)):
        enc = AnnexBEncoder(width, height, 2_000_000)
        rng = np.random.default_rng(3)
        for _ in range(4):
            arr = rng.integers(0, 255, (height, width, 3), dtype=np.uint8)
            for _is_key, _ts, data in enc.encode(
                av.VideoFrame.from_ndarray(arr, format="rgb24")
            ):
                found = _slice_count(data)
                assert found == 1, (
                    f"{width}x{height} produced {found} slices in one chunk; "
                    f"Safari will decode only the first of them"
                )


def test_a_keyframe_still_carries_its_parameter_sets():
    """One slice per frame must not cost the SPS/PPS that go with it."""
    import av
    import numpy as np

    from machop.relay import AnnexBEncoder, codec_string_from_annexb

    enc = AnnexBEncoder(640, 400, 2_000_000)
    arr = np.random.default_rng(4).integers(0, 255, (400, 640, 3), dtype=np.uint8)
    packets = enc.encode(av.VideoFrame.from_ndarray(arr, format="rgb24"))
    assert packets and packets[0][0], "first packet should be a keyframe"
    assert codec_string_from_annexb(packets[0][2]), "keyframe lost its SPS"


def test_frames_are_emitted_without_buffering():
    """One slice per frame must not cost latency."""
    import av
    import numpy as np

    from machop.relay import AnnexBEncoder

    enc = AnnexBEncoder(1290, 838, 2_000_000)
    rng = np.random.default_rng(5)
    for n in range(6):
        arr = rng.integers(0, 255, (838, 1290, 3), dtype=np.uint8)
        packets = enc.encode(av.VideoFrame.from_ndarray(arr, format="rgb24"))
        assert packets, f"frame {n} produced no packet; the encoder is buffering"


def test_the_relay_encoder_actually_preserves_the_picture():
    """The test that was missing, and the reason a broken relay shipped."""
    import math

    import av
    import numpy as np

    from machop.relay import AnnexBEncoder

    width, height = 640, 416
    rng = np.random.default_rng(1)
    source = np.zeros((height, width, 3), np.uint8)
    for y in range(0, height, 24):
        source[y:y + 12, :] = rng.integers(0, 255, 3)
    source[:, ::40] = 255

    enc = AnnexBEncoder(width, height, 4_000_000)
    dec = av.CodecContext.create("h264", "r")
    best = 0.0
    for _ in range(8):
        frame = av.VideoFrame.from_ndarray(source, format="rgb24")
        for _is_key, _ts, data in enc.encode(frame):
            for out in dec.decode(av.Packet(data)):
                got = out.to_ndarray(format="rgb24")
                mse = np.mean((source.astype(np.float64) - got.astype(np.float64)) ** 2)
                score = 99.0 if mse == 0 else 10 * math.log10(255 * 255 / mse)
                best = max(best, score)
    assert best > 25.0, (
        f"best PSNR {best:.2f} dB - the relay is not sending a usable "
        f"picture. Below about 10 dB it is a flat grey wash."
    )


def test_the_encoder_knows_its_frame_rate():
    """Directly, because the failure above is invisible in the output."""
    import fractions

    from machop.relay import AnnexBEncoder

    enc = AnnexBEncoder(640, 416, 4_000_000, fps=30)
    assert enc._context.framerate == fractions.Fraction(30, 1)
    assert enc._context.time_base == fractions.Fraction(1, 30), (
        "a 90 kHz time base makes x264 believe it is encoding 90,000 fps"
    )


async def test_the_relay_encoder_is_told_the_rate_it_will_actually_send():
    """x264 budgets bits per frame from the declared rate."""
    import aiohttp

    from machop.capture import SwitchableSource
    from machop.inputs import InputController
    from machop.security import SessionAuth
    from machop.server import SignalingServer, start_server

    source = SwitchableSource(640, 400, 60)
    await source.start()
    built: list[int] = []
    import machop.server as server_module

    real = server_module.AnnexBEncoder

    def spy(width, height, bitrate, fps=30):
        built.append(fps)
        return real(width, height, bitrate, fps=fps)

    server_module.AnnexBEncoder = spy
    server = SignalingServer(
        auth=SessionAuth(pin="424242"),
        source=source,
        controller=InputController(1470, 956, enabled=False),
        ice_servers=[],
    )
    runner = await start_server(server, port=19004, host="127.0.0.1")
    try:
        client = RelaySession("424242", role="client")
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect("http://127.0.0.1:19004/relay") as ws:
                await ws.send_str(json.dumps({
                    "pin": "424242",
                    "pub": base64.b64encode(client.public_key_bytes).decode(),
                    "w": 4096, "h": 4096,
                }))
                hello = json.loads(await ws.receive_str())
                client.salt = base64.b64decode(hello["salt"])
                client.establish(base64.b64decode(hello["pub"]))
                await ws.send_bytes(client.seal(TAG_READY, b""))
                deadline = asyncio.get_running_loop().time() + 8
                while not built and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.2)
        assert built, "no encoder was ever built"
        from machop.server import RELAY_MAX_FPS

        assert built[0] == int(RELAY_MAX_FPS), (
            f"encoder told {built[0]} fps; capture runs at 60 but the relay "
            f"sends at most {RELAY_MAX_FPS}"
        )
    finally:
        server_module.AnnexBEncoder = real
        await server.close()
        await runner.cleanup()
        await source.stop()


async def test_a_latency_probe_is_echoed():
    """The relay has no getStats(), so a round trip over the video socket is the only way to see the queue the video is sitting in."""
    import aiohttp

    from machop.capture import SwitchableSource
    from machop.inputs import InputController
    from machop.security import SessionAuth
    from machop.server import SignalingServer, start_server

    source = SwitchableSource(640, 400, 30)
    await source.start()
    server = SignalingServer(
        auth=SessionAuth(pin="424242"),
        source=source,
        controller=InputController(1470, 956, enabled=False),
        ice_servers=[],
    )
    runner = await start_server(server, port=19005, host="127.0.0.1")
    try:
        client = RelaySession("424242", role="client")
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect("http://127.0.0.1:19005/relay") as ws:
                await ws.send_str(json.dumps({
                    "pin": "424242",
                    "pub": base64.b64encode(client.public_key_bytes).decode(),
                    "w": 4096, "h": 4096,
                }))
                hello = json.loads(await ws.receive_str())
                client.salt = base64.b64decode(hello["salt"])
                client.establish(base64.b64decode(hello["pub"]))
                await ws.send_bytes(client.seal(TAG_READY, b""))
                await ws.send_bytes(client.seal(
                    TAG_INPUT, json.dumps({"t": "pg", "i": 77}).encode()))

                echoed = None
                deadline = asyncio.get_running_loop().time() + 8
                while echoed is None and asyncio.get_running_loop().time() < deadline:
                    message = await asyncio.wait_for(ws.receive(), timeout=5)
                    if message.type is not aiohttp.WSMsgType.BINARY:
                        continue
                    tag, payload = client.open(message.data)
                    if tag == TAG_INPUT:
                        body = json.loads(payload.decode())
                        if body.get("t") == "pg":
                            echoed = body.get("i")
                assert echoed == 77, "the probe was not echoed back"
    finally:
        await server.close()
        await runner.cleanup()
        await source.stop()


def test_the_encoder_caps_its_instantaneous_rate():
    """An average bitrate is not a cap."""
    from machop.relay import AnnexBEncoder

    enc = AnnexBEncoder(1440, 928, 4_000_000, fps=30)
    params = enc._context.options["x264-params"]
    assert "vbv-maxrate=4000" in params, params
    assert "vbv-bufsize=" in params, params
    bufsize = int(params.split("vbv-bufsize=")[1].split(":")[0])
    assert bufsize <= 4000 // 4, (
        f"buffer of {bufsize} kbit is {bufsize / 4000:.2f}s of video, and its "
        f"size is the worst-case latency it adds"
    )


def test_peak_frame_size_stays_bounded():
    """The measurement that found the lag, as a test."""
    import av
    import numpy as np

    from machop.relay import AnnexBEncoder

    width, height = 1280, 832
    enc = AnnexBEncoder(width, height, 4_000_000, fps=30)
    rng = np.random.default_rng(9)
    biggest = 0
    for _ in range(20):
        arr = rng.integers(0, 255, (height, width, 3), dtype=np.uint8)
        for _is_key, _ts, data in enc.encode(
            av.VideoFrame.from_ndarray(arr, format="rgb24")
        ):
            biggest = max(biggest, len(data))
    assert biggest < 80_000, (
        f"largest frame {biggest / 1024:.0f} KB; unbounded peaks are what put "
        f"seconds of queue on a mobile link"
    )


def test_the_encoder_takes_nv12_without_converting_it():
    """ScreenCaptureKit delivers NV12 and libx264 accepts NV12."""
    from machop.relay import AnnexBEncoder

    encoder = AnnexBEncoder(640, 416, 4_000_000)
    assert encoder.pix_fmt == "nv12"
    assert encoder._context.pix_fmt == "nv12"
    encoder.close()


def test_nv12_frames_still_make_a_picture():
    """The same PSNR check as above, but fed the format the Mac actually produces."""
    import math

    import numpy as np

    from machop.relay import AnnexBEncoder

    width, height = 640, 416
    rng = np.random.default_rng(4)
    luma = np.full((height, width), 235, np.uint8)
    for y in range(0, height, 24):
        luma[y : y + 10, :] = rng.integers(0, 90)
    luma[:, ::40] = 16
    luma[:, 1::40] = 16

    encoder = AnnexBEncoder(width, height, 4_000_000)
    decoder = av.CodecContext.create("h264", "r")
    best = 0.0
    for _ in range(8):
        frame = av.VideoFrame(width, height, "nv12")
        stride = frame.planes[0].line_size
        padded = np.zeros((height, stride), np.uint8)
        padded[:, :width] = luma
        frame.planes[0].update(padded.tobytes())
        frame.planes[1].update(
            np.full((height // 2, frame.planes[1].line_size), 128, np.uint8).tobytes()
        )
        for _is_key, _ts, data in encoder.encode(frame):
            for out in decoder.decode(av.Packet(data)):
                got = out.to_ndarray()[:height, :width].astype(np.float64)
                mse = float(np.mean((luma.astype(np.float64) - got) ** 2))
                score = 99.0 if mse == 0 else 10 * math.log10(255 * 255 / mse)
                best = max(best, score)
    encoder.close()
    assert best > 30.0, f"best PSNR {best:.2f} dB from NV12 input"


async def test_sound_reaches_the_viewer_over_the_relay(monkeypatch, unused_tcp_port):
    """Sound shares the video's socket, so this is the whole path: button, capture, Opus, sealed record, and the 8-byte timestamp the viewer's decoder is scheduled against."""
    import aiohttp
    import numpy as np

    import machop.audio as audio_module
    from machop.relay import TAG_AUDIO

    class FakeAudio:
        def __init__(self):
            self.at = 0

        async def start(self):
            return None

        async def stop(self):
            return None

        async def read(self):
            await asyncio.sleep(0.01)
            self.at += 960
            phase = (np.arange(self.at - 960, self.at) / 48000.0) * 2 * np.pi * 440
            return np.repeat((0.3 * np.sin(phase)).astype(np.float32), 2)

    monkeypatch.setattr(audio_module, "SystemAudioSource", FakeAudio)

    geometry = display_geometry()
    size = target_size(geometry, 640)
    source = await create_frame_source(size[0], size[1], 30)
    controller = InputController(geometry.width, geometry.height, enabled=False)
    server = SignalingServer(
        auth=SessionAuth(pin=PIN), source=source, controller=controller, ice_servers=[]
    )
    runner = await start_server(server, port=unused_tcp_port)
    client = RelaySession(PIN, role="client")
    audio_records = []
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(
                f"http://127.0.0.1:{unused_tcp_port}/relay"
            ) as ws:
                await ws.send_json({
                    "pin": PIN,
                    "pub": base64.b64encode(client.public_key_bytes).decode(),
                })
                hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
                client.salt = base64.b64decode(hello["salt"])
                client.establish(base64.b64decode(hello["pub"]))
                await ws.send_bytes(client.seal(TAG_READY, b""))
                await ws.send_bytes(
                    client.seal(TAG_INPUT, json.dumps({"t": "au", "on": True}).encode())
                )

                deadline = asyncio.get_running_loop().time() + 20
                while (
                    len(audio_records) < 3
                    and asyncio.get_running_loop().time() < deadline
                ):
                    message = await asyncio.wait_for(ws.receive(), timeout=15)
                    if message.type is not aiohttp.WSMsgType.BINARY:
                        continue
                    tag, payload = client.open(message.data)
                    if tag == TAG_AUDIO:
                        audio_records.append(payload)
    finally:
        await server.close()
        await runner.cleanup()
        await source.stop()

    assert len(audio_records) >= 3, "no sound arrived over the relay"
    stamps = [int.from_bytes(record[:8], "big") for record in audio_records]
    assert stamps == sorted(stamps), "audio timestamps went backwards"
    assert stamps[1] - stamps[0] == 20000, "packets are not 20 ms apart"
    assert all(len(record) > 8 for record in audio_records), "empty audio packets"


async def test_no_audio_means_the_relay_never_carries_any(monkeypatch, unused_tcp_port):
    """--no-audio is a refusal on the Mac, not a hint to the viewer."""
    import aiohttp

    import machop.audio as audio_module
    from machop.relay import TAG_AUDIO

    class Refuse:
        async def start(self):
            raise AssertionError("audio was started despite --no-audio")

    monkeypatch.setattr(audio_module, "SystemAudioSource", Refuse)

    geometry = display_geometry()
    size = target_size(geometry, 640)
    source = await create_frame_source(size[0], size[1], 30)
    controller = InputController(geometry.width, geometry.height, enabled=False)
    server = SignalingServer(
        auth=SessionAuth(pin=PIN),
        source=source,
        controller=controller,
        ice_servers=[],
        audio_allowed=False,
    )
    runner = await start_server(server, port=unused_tcp_port)
    client = RelaySession(PIN, role="client")
    answers, audio_seen = [], 0
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(
                f"http://127.0.0.1:{unused_tcp_port}/relay"
            ) as ws:
                await ws.send_json({
                    "pin": PIN,
                    "pub": base64.b64encode(client.public_key_bytes).decode(),
                })
                hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
                client.salt = base64.b64decode(hello["salt"])
                client.establish(base64.b64decode(hello["pub"]))
                await ws.send_bytes(client.seal(TAG_READY, b""))
                await ws.send_bytes(
                    client.seal(TAG_INPUT, json.dumps({"t": "au", "on": True}).encode())
                )
                deadline = asyncio.get_running_loop().time() + 8
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        message = await asyncio.wait_for(ws.receive(), timeout=3)
                    except asyncio.TimeoutError:
                        break
                    if message.type is not aiohttp.WSMsgType.BINARY:
                        continue
                    tag, payload = client.open(message.data)
                    if tag == TAG_AUDIO:
                        audio_seen += 1
                    elif tag == TAG_INPUT:
                        answers.append(json.loads(payload))
                        break
    finally:
        await server.close()
        await runner.cleanup()
        await source.stop()

    assert audio_seen == 0
    assert answers and answers[0]["t"] == "au" and answers[0]["on"] is False
    assert "--no-audio" in answers[0]["m"]
