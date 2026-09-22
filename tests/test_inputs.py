import pytest

pytest.importorskip("Quartz", reason="macOS only")

from machop.inputs import KEY_CODES, MODIFIER_FLAGS, InputController


@pytest.fixture
def controller():
    return InputController(display_width=1470, display_height=956, enabled=False)


def test_normalised_origin_maps_to_origin(controller):
    assert controller._point(0.0, 0.0) == (0.0, 0.0)


def test_normalised_far_corner_stays_inside_display(controller):
    x, y = controller._point(1.0, 1.0)
    assert x == 1469.0 and y == 955.0


@pytest.mark.parametrize("x,y", [(-1.0, 0.5), (2.0, 0.5), (0.5, -3.0), (0.5, 9.0)])
def test_out_of_range_coordinates_are_clamped(controller, x, y):
    px, py = controller._point(x, y)
    assert 0.0 <= px <= 1469.0 and 0.0 <= py <= 955.0


def test_midpoint_maps_to_centre(controller):
    assert controller._point(0.5, 0.5) == (735.0, 478.0)


def test_modifier_flags_accumulate(controller):
    import Quartz

    controller._held_modifiers.update({"MetaLeft", "ShiftLeft"})
    flags = controller._flags()
    assert flags & Quartz.kCGEventFlagMaskCommand
    assert flags & Quartz.kCGEventFlagMaskShift


def test_release_all_clears_modifiers(controller):
    controller._held_modifiers.add("MetaLeft")
    controller.release_all()
    assert not controller._held_modifiers


def test_unmapped_key_reports_failure(controller):
    controller.enabled = True
    assert controller.key("KeyNotReal", True) is False


def test_every_modifier_has_a_keycode():
    """A modifier we can flag but not press would leave input stuck."""
    assert set(MODIFIER_FLAGS) <= set(KEY_CODES)


def test_keymap_covers_the_basics():
    for code in ("KeyA", "Digit0", "Enter", "Space", "Tab", "Escape",
                 "ArrowUp", "Backspace", "MetaLeft", "F1"):
        assert code in KEY_CODES


def test_disabled_controller_dispatches_nothing(controller):
    assert controller.key("KeyA", True) is False


def test_modifier_held_routes_characters_through_keycodes(controller, monkeypatch):
    """Cmd+C must copy, not type the letter c."""
    controller.enabled = True
    posted: list[tuple[str, bool]] = []
    unicode_used: list[str] = []

    monkeypatch.setattr(controller, "key", lambda code, down: (posted.append((code, down)), True)[1])
    import machop.inputs as inputs

    monkeypatch.setattr(
        inputs.Quartz, "CGEventKeyboardSetUnicodeString",
        lambda event, length, text: unicode_used.append(text),
    )

    controller._held_modifiers.add("MetaLeft")
    controller.text("c")
    assert posted == [("KeyC", True), ("KeyC", False)]
    assert unicode_used == [], "must not fall back to the Unicode path"


def test_plain_text_still_uses_the_unicode_path(controller, monkeypatch):
    """Without a modifier, literal insertion must be preserved - it is what makes accented and non-Latin characters work from a soft keyboard."""
    controller.enabled = True
    unicode_used: list[str] = []
    import machop.inputs as inputs

    monkeypatch.setattr(
        inputs.Quartz, "CGEventKeyboardSetUnicodeString",
        lambda event, length, text: unicode_used.append(text),
    )
    monkeypatch.setattr(inputs.Quartz, "CGEventPost", lambda tap, event: None)

    controller.text("é")
    assert unicode_used == ["é", "é"]


def test_unmappable_character_with_modifier_is_dropped(controller, monkeypatch):
    """Better to drop it than insert a stray character into a shell."""
    controller.enabled = True
    posted: list = []
    monkeypatch.setattr(controller, "key", lambda code, down: (posted.append(code), True)[1])
    controller._held_modifiers.add("MetaLeft")
    controller.text("©")
    assert posted == []


async def test_chord_presses_in_order_and_releases_in_reverse(controller, monkeypatch):
    """Cmd+Tab: the modifier must go down first and come up last, with the switcher given time to appear - a single-tick chord does nothing."""
    controller.enabled = True
    sequence: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        controller, "key", lambda code, down: (sequence.append((code, down)), True)[1]
    )
    assert await controller.chord(["MetaLeft", "Tab"]) is True
    assert sequence == [
        ("MetaLeft", True), ("Tab", True), ("Tab", False), ("MetaLeft", False),
    ]


async def test_chord_rejects_unknown_keys(controller):
    controller.enabled = True
    assert await controller.chord(["MetaLeft", "KeyNotReal"]) is False


async def test_chord_releases_even_if_a_press_raises(controller, monkeypatch):
    """A stuck Cmd key would make the Mac unusable until restart."""
    controller.enabled = True
    sequence: list[tuple[str, bool]] = []

    def flaky(code, down):
        sequence.append((code, down))
        if code == "Tab" and down:
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(controller, "key", flaky)
    with pytest.raises(RuntimeError):
        await controller.chord(["MetaLeft", "Tab"])
    assert ("MetaLeft", False) in sequence, "modifier was left held down"


async def test_disabled_controller_ignores_chords(controller):
    assert await controller.chord(["MetaLeft", "Tab"]) is False


def test_a_dropped_connection_releases_a_held_mouse_button(controller, monkeypatch):
    """Losing the link mid-drag must not leave the Mac with the button down; it would be unusable until someone physically clicked."""
    controller.enabled = True
    posted: list = []
    import machop.inputs as inputs

    monkeypatch.setattr(inputs.Quartz, "CGEventPost", lambda tap, event: posted.append(event))

    controller.mouse(0.5, 0.5, "down", "left")
    assert controller.held_buttons == frozenset({"left"})

    posted.clear()
    controller.release_all()
    assert controller.held_buttons == frozenset()
    assert posted, "no release event was posted"


def test_normal_release_clears_the_held_button(controller):
    controller.enabled = True
    controller.mouse(0.5, 0.5, "down", "left")
    controller.mouse(0.5, 0.5, "up", "left")
    assert controller.held_buttons == frozenset()


def test_drag_keeps_the_button_marked_held(controller):
    controller.enabled = True
    controller.mouse(0.2, 0.2, "down", "left")
    controller.mouse(0.6, 0.6, "drag", "left")
    assert controller.held_buttons == frozenset({"left"})


def test_release_all_clears_buttons_and_modifiers_together(controller, monkeypatch):
    controller.enabled = True
    import machop.inputs as inputs

    monkeypatch.setattr(inputs.Quartz, "CGEventPost", lambda tap, event: None)
    controller.mouse(0.5, 0.5, "down", "right")
    controller._held_modifiers.add("MetaLeft")
    controller.release_all()
    assert controller.held_buttons == frozenset()
    assert controller._held_modifiers == set()


def test_release_all_is_safe_when_nothing_is_held(controller):
    controller.release_all()
    controller.release_all()


def test_scroll_marks_the_event_continuous_and_phased(controller, monkeypatch):
    """Without IsContinuous and a phase, macOS treats every event as a notched mouse wheel: it jumps, never rubber-bands and never decelerates."""
    import machop.inputs as inputs

    fields: dict[int, int] = {}
    monkeypatch.setattr(inputs.Quartz, "CGEventPost", lambda tap, event: None)
    monkeypatch.setattr(
        inputs.Quartz, "CGEventSetIntegerValueField",
        lambda event, field, value: fields.__setitem__(field, value),
    )
    controller.enabled = True
    controller.scroll(0, -40, "begin")

    import Quartz

    assert fields.get(Quartz.kCGScrollWheelEventIsContinuous) == 1
    assert fields.get(Quartz.kCGScrollWheelEventScrollPhase) == Quartz.kCGScrollPhaseBegan


def test_scroll_end_is_delivered_even_with_zero_delta(controller, monkeypatch):
    """The closing phase carries no movement but must still arrive, or the gesture never finishes and scrolling stops dead instead of decelerating."""
    import machop.inputs as inputs

    posted: list = []
    monkeypatch.setattr(inputs.Quartz, "CGEventPost", lambda tap, event: posted.append(event))
    monkeypatch.setattr(inputs.Quartz, "CGEventSetIntegerValueField", lambda *a: None)
    controller.enabled = True
    controller.scroll(0, 0, "end")
    assert posted, "the end phase was swallowed"


def test_zero_delta_mid_gesture_is_skipped(controller, monkeypatch):
    import machop.inputs as inputs

    posted: list = []
    monkeypatch.setattr(inputs.Quartz, "CGEventPost", lambda tap, event: posted.append(event))
    controller.enabled = True
    controller.scroll(0, 0, "move")
    assert posted == []


async def test_system_gestures_map_to_their_shortcuts(controller, monkeypatch):
    """Trackpad swipes cannot be synthesised through CGEvent; these are the same actions' default keyboard shortcuts."""
    controller.enabled = True
    sent: list = []
    monkeypatch.setattr(
        controller, "key", lambda code, down: (sent.append((code, down)), True)[1]
    )
    assert await controller.gesture("mission") is True
    assert ("ControlLeft", True) in sent and ("ArrowUp", True) in sent


async def test_unknown_gesture_is_ignored(controller):
    controller.enabled = True
    assert await controller.gesture("nonsense") is False


async def test_hold_chord_leaves_the_modifier_down(controller, monkeypatch):
    """The application switcher only walks past two apps if Cmd stays held."""
    controller.enabled = True
    sent: list = []
    monkeypatch.setattr(
        controller, "key", lambda code, down: (sent.append((code, down)), True)[1]
    )
    assert await controller.hold_chord(["MetaLeft", "Tab"]) is True
    assert sent == [("MetaLeft", True), ("Tab", True), ("Tab", False)]
    assert ("MetaLeft", False) not in sent, "Cmd was released; switcher would close"
