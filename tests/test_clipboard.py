"""Clipboard bridging: copy on the Mac, land it on the phone, and back."""

import asyncio
import json

import pytest

pytest.importorskip("Quartz", reason="macOS only")

from machop import clipboard
from machop.capture import SwitchableSource
from machop.clipboard import MAX_CLIPBOARD_CHARS
from machop.inputs import InputController
from machop.security import SessionAuth
from machop.server import SignalingServer


@pytest.fixture
def preserve_clipboard():
    """Never leave the developer's own clipboard trashed by a test run."""
    before = clipboard.read_text()
    yield
    if before is not None:
        clipboard.write_text(before)


def test_roundtrip(preserve_clipboard):
    assert clipboard.write_text("machop test value")
    assert clipboard.read_text() == "machop test value"


def test_unicode_survives(preserve_clipboard):
    value = "café — ✓ 日本語 \n second line"
    assert clipboard.write_text(value)
    assert clipboard.read_text() == value


def test_an_oversized_write_is_refused(preserve_clipboard):
    """The data channel is ordered and reliable, so a huge paste would stall every keystroke queued behind it."""
    assert clipboard.write_text("x" * (MAX_CLIPBOARD_CHARS + 1)) is False


def test_a_non_string_is_refused(preserve_clipboard):
    assert clipboard.write_text(None) is False
    assert clipboard.write_text(12345) is False


def test_an_oversized_read_is_truncated(preserve_clipboard):
    clipboard.write_text("y" * (MAX_CLIPBOARD_CHARS - 1))
    assert len(clipboard.read_text()) == MAX_CLIPBOARD_CHARS - 1


def _server(source, enabled=True):
    return SignalingServer(
        auth=SessionAuth(pin="424242"),
        source=source,
        controller=InputController(1470, 956, enabled=enabled),
        ice_servers=[],
    )


async def test_copy_replies_with_the_mac_clipboard(preserve_clipboard, monkeypatch):
    """The reply is the whole point: copying onto a Mac you are not sitting at achieves nothing by itself."""
    source = SwitchableSource(640, 400, 30)
    await source.start()
    server = _server(source)
    sent: list[str] = []
    chords: list[list[str]] = []

    async def fake_chord(codes):
        chords.append(list(codes))
        return True

    monkeypatch.setattr(server.state.controller, "chord", fake_chord)
    monkeypatch.setattr(clipboard, "read_text", lambda: "a stack trace")
    try:
        server._handle_input(json.dumps({"t": "cb", "a": "copy"}), sent.append)
        for _ in range(40):
            if sent:
                break
            await asyncio.sleep(0.05)
        assert chords == [["MetaLeft", "KeyC"]]
        assert json.loads(sent[0]) == {"t": "cb", "v": "a stack trace"}
    finally:
        await source.stop()


async def test_copy_with_an_empty_clipboard_still_answers(preserve_clipboard, monkeypatch):
    """Silence would leave the viewer showing 'Copying…' forever."""
    source = SwitchableSource(640, 400, 30)
    await source.start()
    server = _server(source)
    sent: list[str] = []

    async def fake_chord(codes):
        return True

    monkeypatch.setattr(server.state.controller, "chord", fake_chord)
    monkeypatch.setattr(clipboard, "read_text", lambda: None)
    try:
        server._handle_input(json.dumps({"t": "cb", "a": "copy"}), sent.append)
        for _ in range(40):
            if sent:
                break
            await asyncio.sleep(0.05)
        assert json.loads(sent[0]) == {"t": "cb", "v": ""}
    finally:
        await source.stop()


async def test_paste_sets_the_clipboard_then_presses_cmd_v(preserve_clipboard, monkeypatch):
    """Via the pasteboard, not synthesised keystrokes: typing a stack trace character by character is slow and mangles the newlines."""
    source = SwitchableSource(640, 400, 30)
    await source.start()
    server = _server(source)
    chords: list[list[str]] = []
    written: list[str] = []

    async def fake_chord(codes):
        chords.append(list(codes))
        return True

    monkeypatch.setattr(server.state.controller, "chord", fake_chord)
    monkeypatch.setattr(clipboard, "write_text", lambda v: written.append(v) or True)
    try:
        server._handle_input(json.dumps({"t": "cb", "a": "paste", "v": "hello\nthere"}))
        for _ in range(40):
            if chords:
                break
            await asyncio.sleep(0.05)
        assert written == ["hello\nthere"]
        assert chords == [["MetaLeft", "KeyV"]]
    finally:
        await source.stop()


async def test_clipboard_is_refused_in_view_only_mode(preserve_clipboard, monkeypatch):
    """--view-only must not be a way to read the Mac's clipboard."""
    source = SwitchableSource(640, 400, 30)
    await source.start()
    server = _server(source, enabled=False)
    sent: list[str] = []
    monkeypatch.setattr(clipboard, "read_text", lambda: "secret")
    try:
        server._handle_input(json.dumps({"t": "cb", "a": "copy"}), sent.append)
        await asyncio.sleep(0.4)
        assert sent == []
    finally:
        await source.stop()


async def test_an_empty_paste_does_nothing(preserve_clipboard, monkeypatch):
    source = SwitchableSource(640, 400, 30)
    await source.start()
    server = _server(source)
    chords: list[list[str]] = []

    async def fake_chord(codes):
        chords.append(list(codes))
        return True

    monkeypatch.setattr(server.state.controller, "chord", fake_chord)
    try:
        server._handle_input(json.dumps({"t": "cb", "a": "paste", "v": ""}))
        await asyncio.sleep(0.3)
        assert chords == []
    finally:
        await source.stop()


async def test_a_copy_with_no_reply_channel_does_not_raise(preserve_clipboard, monkeypatch):
    """The viewer can vanish between the request and the answer."""
    source = SwitchableSource(640, 400, 30)
    await source.start()
    server = _server(source)

    async def fake_chord(codes):
        return True

    monkeypatch.setattr(server.state.controller, "chord", fake_chord)
    try:
        server._handle_input(json.dumps({"t": "cb", "a": "copy"}), None)
        await asyncio.sleep(0.4)
    finally:
        await source.stop()
