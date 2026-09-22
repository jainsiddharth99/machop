"""Static and behavioural checks on the browser client."""

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).parent.parent / "src" / "machop" / "web"
APP_JS = (WEB / "app.js").read_text()
INDEX = (WEB / "index.html").read_text()


def test_offer_declares_a_recvonly_video_transceiver():
    """Without this the offer has no m=video line and the Mac returns 400."""
    assert "addTransceiver('video', { direction: 'recvonly' })" in APP_JS


def test_ice_servers_are_configured():
    assert "stun:stun.l.google.com:19302" in APP_JS


def test_pointer_motion_is_coalesced_to_animation_frames():
    """Sending every pointermove queues behind itself and adds latency."""
    assert "requestAnimationFrame" in APP_JS


def test_all_input_kinds_are_wired():
    for event in ("pointerdown", "pointermove", "pointerup", "wheel", "keydown", "keyup"):
        assert event in APP_JS, f"{event} is not handled"


def test_soft_keyboard_path_exists():
    """iOS reports code:'' for on-screen keys; only the inserted text works."""
    assert "typer" in APP_JS and "'x'" in APP_JS
    assert 'id="typer"' in INDEX


def test_relay_verifies_the_confirmation_tag():
    """A relay that swaps the key must be detected, not silently trusted."""
    assert "machop confirm" in APP_JS
    assert "refusing to connect" in APP_JS


def test_relay_derives_all_four_key_materials():
    for info in ("machop s2c", "machop c2s",
                 "machop nonce s2c", "machop nonce c2s"):
        assert info in APP_JS, f"missing KDF label {info}"


def test_relay_decoder_optimises_for_latency():
    assert "optimizeForLatency: true" in APP_JS


def test_ice_failure_falls_back_rather_than_giving_up():
    assert "connectRelay" in APP_JS
    assert "state !== 'failed'" in APP_JS or "state === 'failed'" in APP_JS


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_coordinate_mapping_via_node():
    result = subprocess.run(
        ["node", str(Path(__file__).parent / "web" / "normalise.test.mjs")],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout


def test_arrow_keys_are_on_the_toolbar():
    """A phone keyboard has no arrows, and terminals need them constantly."""
    for code in ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"):
        assert f'data-key="{code}"' in INDEX, f"{code} missing from the toolbar"


def test_ctrl_c_is_a_single_button():
    """The one shortcut this tool exists for: stop a runaway job."""
    assert 'data-combo="ControlLeft+KeyC"' in INDEX
    assert "sendCombo" in APP_JS


def test_toolbar_keys_are_all_mappable():
    """A button that sends an unmapped code does nothing, silently."""
    import re

    from machop.inputs import KEY_CODES

    codes = re.findall(r'data-key="([^"]+)"', INDEX)
    assert codes, "no data-key buttons found"
    for code in codes:
        assert code in KEY_CODES, f"{code} has no macOS keycode"

    combos = re.findall(r'data-combo="([^"]+)"', INDEX)
    assert combos, "no data-combo buttons found"
    for combo in combos:
        for code in combo.split("+"):
            assert code in KEY_CODES, f"{code} has no macOS keycode"


def test_sentinel_keeps_backspace_firing():
    """With an empty field iOS may not fire deleteContentBackward at all."""
    assert "SENTINEL" in APP_JS
    assert "beforeinput" in APP_JS


def test_quality_toggle_sends_a_profile_message():
    assert "t: 'q'" in APP_JS
    assert 'id="quality"' in INDEX


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_soft_keyboard_mapping_via_node():
    result = subprocess.run(
        ["node", str(Path(__file__).parent / "web" / "keyboard.test.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_gesture_engine_via_node():
    result = subprocess.run(
        ["node", str(Path(__file__).parent / "web" / "gesture.test.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout


def test_touch_is_routed_through_the_gesture_engine():
    """A one-finger drag mapped straight to a mouse drag selects text when the user meant to scroll - the first thing a real phone exposed."""
    assert "createGestureEngine" in APP_JS
    assert "pointerType === 'touch'" in APP_JS


def test_mouse_keeps_direct_handling_so_hover_still_works():
    """Desktop viewers need hover for menus and tooltips; routing a mouse through a touch gesture engine would swallow it."""
    assert "isTouch(event)" in APP_JS
    assert "mouseDown ? 'drag' : 'move'" in APP_JS


def test_named_keys_include_enter():
    """Return from an iOS soft keyboard arrives as keydown on the hidden input and nowhere else: a single-line <input> never fires beforeinput('insertLineBreak')."""
    assert "NAMED_KEYS" in APP_JS
    for key in ("Enter", "Backspace", "Tab", "Escape", "ArrowUp"):
        assert f"{key}:" in APP_JS, f"{key} missing from NAMED_KEYS"
    assert "typer.addEventListener('keydown'" in APP_JS


def test_app_switcher_is_a_mode_not_a_one_shot_chord():
    """Pressing and releasing Cmd+Tab only ever toggles the last two apps."""
    assert 'id="switch"' in INDEX
    assert "h: true" in APP_JS, "hold flag missing; switcher will not stay open"
    assert "commitSwitcher" in APP_JS


def test_spotlight_chord_is_present():
    assert 'data-chord="MetaLeft+Space"' in INDEX
    assert "t: 'c'" in APP_JS


def test_three_finger_swipes_map_to_system_gestures():
    """Mission Control is how people actually switch apps on a Mac."""
    assert "SWIPE_ACTIONS" in APP_JS
    for direction in ("up", "down", "left", "right"):
        assert f"{direction}:" in APP_JS
    assert "t: 'g'" in APP_JS


def test_swipe_targets_exist_on_the_mac_side():
    import re

    from machop.inputs import GESTURE_CHORDS

    block = APP_JS[APP_JS.index("SWIPE_ACTIONS"):]
    block = block[: block.index("};")]
    for name in re.findall(r"'([a-z-]+)'", block):
        assert name in GESTURE_CHORDS, f"{name} has no handler on the Mac"


def test_scroll_sends_begin_and_end_phases():
    """Without the closing phase AppKit never decelerates or rubber-bands; scrolling stops dead instead of feeling like a trackpad."""
    assert "phase: 'end'" in APP_JS
    assert "p: action.phase" in APP_JS


def test_scroll_sensitivity_is_adjustable_and_persisted():
    assert "SENSITIVITIES" in APP_JS
    assert 'id="sens"' in INDEX
    assert "localStorage" in APP_JS


def test_browser_storage_access_is_guarded():
    """localStorage throws in private mode; the viewer must still work."""
    head = APP_JS[: APP_JS.index("function connect")]
    assert "try {" in head and "catch {}" in head


def test_chord_keys_are_all_mappable():
    import re

    from machop.inputs import KEY_CODES

    chords = re.findall(r'data-chord="([^"]+)"', INDEX)
    assert chords, "no data-chord buttons found"
    for chord in chords:
        for code in chord.split("+"):
            assert code in KEY_CODES, f"{code} has no macOS keycode"


def test_sticky_modifier_is_released_after_one_keystroke():
    """Otherwise cmd stays latched and the next tap does something wild."""
    assert APP_JS.count("releaseModifiers();") >= 3


def test_the_sound_button_exists_and_is_wired():
    """Audio cannot be started outside a user gesture on any mobile browser, so the button is not a preference - it is the only moment the platform will allow an AudioContext to start."""
    assert 'id="sound"' in INDEX
    assert "soundBtn.addEventListener('click'" in APP_JS
    assert "audioEnable" in APP_JS


def test_sound_is_never_started_without_being_asked_for():
    """Nothing may create an AudioContext at load; it must be inside the click handler, or iOS leaves it suspended forever and nothing plays."""
    prologue = APP_JS.split("async function audioEnable()")[0]
    assert "new (window.AudioContext" not in prologue


def test_audio_travels_on_its_own_unreliable_channel():
    """A retransmitted audio packet arrives after the moment it was meant to be heard AND delays the input queued behind it."""
    assert "createDataChannel('audio'" in APP_JS
    assert "ordered: false, maxRetransmits: 0" in APP_JS


def test_both_transports_feed_the_same_decoder():
    """One decode path, or the relay and peer-to-peer drift apart."""
    assert APP_JS.count("onAudioPayload(") >= 3


def test_playback_uses_a_ring_buffer_not_scheduled_buffers():
    """Fifty scheduled AudioBufferSourceNodes a second is fifty chances a second for a click at the seam."""
    assert "registerProcessor('machop-player'" in APP_JS
    assert "AudioWorkletNode" in APP_JS


def test_a_backlog_of_sound_is_dropped_rather_than_played_late():
    """Late audio does not catch up by itself; it stays late against the picture for the rest of the session."""
    assert "this.readPos = this.writePos - this.target" in APP_JS


def test_the_direct_connection_is_given_about_a_second():
    """This is what makes "relay unless we are on the same network" the behaviour rather than a setting: LAN candidates match immediately, and anything still negotiating after this would be worse than the relay."""
    import re

    match = re.search(r"CONNECT_DEADLINE_MS = (\d+)", APP_JS)
    assert match, "the deadline is gone; cellular will hang on ICE"
    assert int(match.group(1)) <= 2500, (
        "too long: this wait happens on every cellular connection"
    )


def test_the_decode_queue_stays_shallow():
    """Every queued frame is another frame-interval between what the Mac is doing and what is on screen."""
    import re

    match = re.search(r"DECODE_QUEUE_LIMIT = (\d+)", APP_JS)
    assert match and int(match.group(1)) <= 3


def test_the_canvas_asks_not_to_wait_for_the_compositor():
    assert "desynchronized: true" in APP_JS
    assert "alpha: false" in APP_JS


def test_the_two_halves_of_the_handshake_agree_on_their_kdf_labels():
    """The relay's keys are derived from these exact strings on both sides.

    Python and the browser each hardcode them, and a rename that touches one
    file and not the other does not fail loudly: the confirmation tag simply
    does not match and the viewer reports that it cannot verify the Mac.
    Nothing in the Python suite would notice, because it tests Python against
    Python.
    """
    import re
    from pathlib import Path

    relay = (Path(__file__).parent.parent / "src" / "machop" / "relay.py").read_text()
    expected = set(re.findall(r'_hkdf\([^)]*?b"([^"]+)"', relay))
    assert len(expected) == 5, f"expected five KDF labels, found {sorted(expected)}"

    found = set(re.findall(r"hkdf\(ikm, salt, '([^']+)'", APP_JS))
    assert found == expected, (
        f"viewer and Mac disagree on the relay KDF labels.\n"
        f"  only in relay.py: {sorted(expected - found)}\n"
        f"  only in app.js:   {sorted(found - expected)}"
    )


def test_the_session_cookie_name_matches_the_server():
    from machop.server import SESSION_COOKIE

    assert SESSION_COOKIE == "machop_session"
