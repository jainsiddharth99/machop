"""Sharing the Mac's sound: who may start it, and when it stops."""

import asyncio
import json

import numpy as np
import pytest

pytest.importorskip("Quartz", reason="macOS only")

import machop.audio as audio_module
from machop.capture import FrameSource
from machop.inputs import InputController
from machop.security import SessionAuth
from machop.server import SignalingServer


class _NoFrames(FrameSource):
    """A source that never produces a picture; these tests are about sound."""

    fps = 30

    def __init__(self):
        super().__init__(640, 400)

    async def start(self):
        return None

    async def stop(self):
        return None

    async def read(self):
        await asyncio.sleep(3600)


class _FakeAudio:
    """Stands in for ScreenCaptureKit. Records whether it was stopped."""

    started = 0
    fail_with = None

    def __init__(self):
        self.stopped = False
        self.at = 0
        _FakeAudio.instances.append(self)

    instances: list = []

    async def start(self):
        if _FakeAudio.fail_with:
            raise RuntimeError(_FakeAudio.fail_with)
        _FakeAudio.started += 1

    async def stop(self):
        self.stopped = True

    async def read(self):
        await asyncio.sleep(0.01)
        self.at += 960
        phase = (np.arange(self.at - 960, self.at) / 48000.0) * 2 * np.pi * 440
        mono = (0.3 * np.sin(phase)).astype(np.float32)
        return np.repeat(mono, 2)


@pytest.fixture(autouse=True)
def fake_audio(monkeypatch):
    _FakeAudio.started = 0
    _FakeAudio.fail_with = None
    _FakeAudio.instances = []
    monkeypatch.setattr(audio_module, "SystemAudioSource", _FakeAudio)
    yield _FakeAudio


def build(audio_allowed=True):
    return SignalingServer(
        auth=SessionAuth(pin="424242"),
        source=_NoFrames(),
        controller=InputController(1470, 956, enabled=False),
        ice_servers=[],
        audio_allowed=audio_allowed,
    )


def replies():
    got = []
    return got, got.append


async def test_sound_is_off_until_a_viewer_asks():
    server = build()
    assert server.state.audio_on is False
    assert _FakeAudio.started == 0
    await server.close()


async def test_the_button_starts_it_and_the_mac_confirms():
    server = build()
    got, reply = replies()
    server._handle_input(json.dumps({"t": "au", "on": True}), reply)
    await asyncio.sleep(0.05)
    assert server.state.audio_on is True
    assert _FakeAudio.started == 1
    assert json.loads(got[-1]) == {"t": "au", "on": True, "m": ""}
    await server.close()


async def test_pressing_it_again_stops_the_capture():
    server = build()
    got, reply = replies()
    server._handle_input(json.dumps({"t": "au", "on": True}), reply)
    await asyncio.sleep(0.05)
    source = _FakeAudio.instances[-1]
    server._handle_input(json.dumps({"t": "au", "on": False}), reply)
    await asyncio.sleep(0.05)
    assert server.state.audio_on is False
    assert source.stopped is True, "the capture was left running"
    assert json.loads(got[-1])["on"] is False
    await server.close()


async def test_no_audio_refuses_and_explains_itself():
    """A button that looks on while nothing plays is worse than a refusal."""
    server = build(audio_allowed=False)
    got, reply = replies()
    server._handle_input(json.dumps({"t": "au", "on": True}), reply)
    await asyncio.sleep(0.05)
    assert server.state.audio_on is False
    assert _FakeAudio.started == 0
    answer = json.loads(got[-1])
    assert answer["on"] is False
    assert "--no-audio" in answer["m"]
    await server.close()


async def test_a_capture_that_will_not_start_is_reported_not_swallowed():
    server = build()
    _FakeAudio.fail_with = "screen recording permission is required"
    got, reply = replies()
    server._handle_input(json.dumps({"t": "au", "on": True}), reply)
    await asyncio.sleep(0.05)
    answer = json.loads(got[-1])
    assert answer["on"] is False
    assert "permission" in answer["m"]
    assert server._audio_source is None
    assert server._audio_task is None
    await server.close()


async def test_sound_stops_when_the_last_viewer_goes():
    """A Mac quietly recording its own audio after everyone has left is exactly what nobody would expect it to be doing."""
    server = build()
    server._viewer_joined()
    got, reply = replies()
    server._handle_input(json.dumps({"t": "au", "on": True}), reply)
    await asyncio.sleep(0.05)
    source = _FakeAudio.instances[-1]
    assert server.state.audio_on is True

    server._viewer_left()
    await asyncio.sleep(0.05)
    assert server.state.audio_on is False
    assert source.stopped is True
    assert server._audio_sinks == set()
    await server.close()


async def test_packets_reach_every_attached_transport():
    """The relay and the peer-to-peer data channel are the same sink type, so audio must not be wired into one path and forgotten in the other."""
    server = build()
    seen: list[bytes] = []

    async def sink(payload: bytes) -> None:
        seen.append(payload)

    server._audio_sinks.add(sink)
    got, reply = replies()
    server._handle_input(json.dumps({"t": "au", "on": True}), reply)
    for _ in range(100):
        await asyncio.sleep(0.02)
        if seen:
            break
    await server.close()
    assert seen, "audio was started but nothing was ever handed to a sink"
    assert len(seen[0]) > 8, "a packet with no payload behind its timestamp"
    assert int.from_bytes(seen[0][:8], "big") == 0, "first packet is not at t=0"


async def test_shutting_down_releases_the_microphone_tap():
    server = build()
    got, reply = replies()
    server._handle_input(json.dumps({"t": "au", "on": True}), reply)
    await asyncio.sleep(0.05)
    source = _FakeAudio.instances[-1]
    await server.close()
    assert source.stopped is True
