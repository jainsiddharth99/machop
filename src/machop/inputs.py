"""Translate viewer input events into native macOS events."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import Quartz

logger = logging.getLogger(__name__)

KEY_CODES: dict[str, int] = {
    "KeyA": 0x00, "KeyB": 0x0B, "KeyC": 0x08, "KeyD": 0x02, "KeyE": 0x0E,
    "KeyF": 0x03, "KeyG": 0x05, "KeyH": 0x04, "KeyI": 0x22, "KeyJ": 0x26,
    "KeyK": 0x28, "KeyL": 0x25, "KeyM": 0x2E, "KeyN": 0x2D, "KeyO": 0x1F,
    "KeyP": 0x23, "KeyQ": 0x0C, "KeyR": 0x0F, "KeyS": 0x01, "KeyT": 0x11,
    "KeyU": 0x20, "KeyV": 0x09, "KeyW": 0x0D, "KeyX": 0x07, "KeyY": 0x10,
    "KeyZ": 0x06,
    "Digit0": 0x1D, "Digit1": 0x12, "Digit2": 0x13, "Digit3": 0x14,
    "Digit4": 0x15, "Digit5": 0x17, "Digit6": 0x16, "Digit7": 0x1A,
    "Digit8": 0x1C, "Digit9": 0x19,
    "Minus": 0x1B, "Equal": 0x18, "BracketLeft": 0x21, "BracketRight": 0x1E,
    "Backslash": 0x2A, "Semicolon": 0x29, "Quote": 0x27, "Backquote": 0x32,
    "Comma": 0x2B, "Period": 0x2F, "Slash": 0x2C,
    "Enter": 0x24, "NumpadEnter": 0x4C, "Tab": 0x30, "Space": 0x31,
    "Backspace": 0x33, "Delete": 0x75, "Escape": 0x35,
    "ArrowUp": 0x7E, "ArrowDown": 0x7D, "ArrowLeft": 0x7B, "ArrowRight": 0x7C,
    "Home": 0x73, "End": 0x77, "PageUp": 0x74, "PageDown": 0x79,
    "CapsLock": 0x39,
    "MetaLeft": 0x37, "MetaRight": 0x36, "ShiftLeft": 0x38, "ShiftRight": 0x3C,
    "AltLeft": 0x3A, "AltRight": 0x3D, "ControlLeft": 0x3B, "ControlRight": 0x3E,
    "F1": 0x7A, "F2": 0x78, "F3": 0x63, "F4": 0x76, "F5": 0x60, "F6": 0x61,
    "F7": 0x62, "F8": 0x64, "F9": 0x65, "F10": 0x6D, "F11": 0x67, "F12": 0x6F,
}

MODIFIER_FLAGS: dict[str, int] = {
    "ShiftLeft": Quartz.kCGEventFlagMaskShift,
    "ShiftRight": Quartz.kCGEventFlagMaskShift,
    "ControlLeft": Quartz.kCGEventFlagMaskControl,
    "ControlRight": Quartz.kCGEventFlagMaskControl,
    "AltLeft": Quartz.kCGEventFlagMaskAlternate,
    "AltRight": Quartz.kCGEventFlagMaskAlternate,
    "MetaLeft": Quartz.kCGEventFlagMaskCommand,
    "MetaRight": Quartz.kCGEventFlagMaskCommand,
}

_BUTTONS = {
    "left": (
        Quartz.kCGMouseButtonLeft,
        Quartz.kCGEventLeftMouseDown,
        Quartz.kCGEventLeftMouseUp,
        Quartz.kCGEventLeftMouseDragged,
    ),
    "right": (
        Quartz.kCGMouseButtonRight,
        Quartz.kCGEventRightMouseDown,
        Quartz.kCGEventRightMouseUp,
        Quartz.kCGEventRightMouseDragged,
    ),
    "middle": (
        Quartz.kCGMouseButtonCenter,
        Quartz.kCGEventOtherMouseDown,
        Quartz.kCGEventOtherMouseUp,
        Quartz.kCGEventOtherMouseDragged,
    ),
}

CHAR_TO_CODE: dict[str, str] = {
    **{chr(c): f"Key{chr(c).upper()}" for c in range(ord("a"), ord("z") + 1)},
    **{chr(c): f"Key{chr(c)}" for c in range(ord("A"), ord("Z") + 1)},
    **{str(d): f"Digit{d}" for d in range(10)},
    " ": "Space", "-": "Minus", "=": "Equal", "[": "BracketLeft",
    "]": "BracketRight", "\\": "Backslash", ";": "Semicolon", "'": "Quote",
    "`": "Backquote", ",": "Comma", ".": "Period", "/": "Slash",
    "\n": "Enter", "\r": "Enter", "\t": "Tab",
}

CHORD_STEP_SECONDS = 0.04
CHORD_HOLD_SECONDS = 0.12

SCROLL_PHASES = {
    "begin": Quartz.kCGScrollPhaseBegan,
    "move": Quartz.kCGScrollPhaseChanged,
    "end": Quartz.kCGScrollPhaseEnded,
}

GESTURE_CHORDS: dict[str, list[str]] = {
    "mission": ["ControlLeft", "ArrowUp"],
    "expose": ["ControlLeft", "ArrowDown"],
    "space-left": ["ControlLeft", "ArrowLeft"],
    "space-right": ["ControlLeft", "ArrowRight"],
}

DOUBLE_CLICK_SECONDS = 0.4
DOUBLE_CLICK_SLOP_POINTS = 6.0


@dataclass
class InputController:
    """Stateful because macOS click-chains and modifiers are stateful."""

    display_width: int
    display_height: int
    enabled: bool = True

    _held_modifiers: set[str] = field(default_factory=set)
    _held_buttons: dict[str, tuple[float, float]] = field(default_factory=dict)
    _last_click_at: float = 0.0
    _last_click_pos: tuple[float, float] = (0.0, 0.0)
    _click_state: int = 1

    def _point(self, x: float, y: float) -> tuple[float, float]:
        px = min(max(float(x), 0.0), 1.0) * self.display_width
        py = min(max(float(y), 0.0), 1.0) * self.display_height
        return (
            min(px, self.display_width - 1.0),
            min(py, self.display_height - 1.0),
        )

    def _flags(self) -> int:
        flags = 0
        for name in self._held_modifiers:
            flags |= MODIFIER_FLAGS.get(name, 0)
        return flags

    def _post(self, event) -> None:
        if event is None:
            return
        flags = self._flags()
        if flags:
            Quartz.CGEventSetFlags(event, Quartz.CGEventGetFlags(event) | flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def _track_click_chain(self, x: float, y: float, now: float) -> int:
        """macOS distinguishes double/triple clicks by an explicit click state."""
        dx = x - self._last_click_pos[0]
        dy = y - self._last_click_pos[1]
        near = (dx * dx + dy * dy) ** 0.5 <= DOUBLE_CLICK_SLOP_POINTS
        if near and (now - self._last_click_at) <= DOUBLE_CLICK_SECONDS:
            self._click_state = min(self._click_state + 1, 3)
        else:
            self._click_state = 1
        self._last_click_at = now
        self._last_click_pos = (x, y)
        return self._click_state

    def mouse(self, x: float, y: float, action: str, button: str = "left") -> None:
        if not self.enabled:
            return
        spec = _BUTTONS.get(button)
        if spec is None:
            return
        cg_button, down, up, dragged = spec
        px, py = self._point(x, y)
        point = Quartz.CGPointMake(px, py)

        if action == "down":
            self._held_buttons[button] = (px, py)
            clicks = self._track_click_chain(px, py, time.monotonic())
            event = Quartz.CGEventCreateMouseEvent(None, down, point, cg_button)
            Quartz.CGEventSetIntegerValueField(
                event, Quartz.kCGMouseEventClickState, clicks
            )
        elif action == "up":
            self._held_buttons.pop(button, None)
            event = Quartz.CGEventCreateMouseEvent(None, up, point, cg_button)
            Quartz.CGEventSetIntegerValueField(
                event, Quartz.kCGMouseEventClickState, self._click_state
            )
        elif action == "drag":
            self._held_buttons[button] = (px, py)
            event = Quartz.CGEventCreateMouseEvent(None, dragged, point, cg_button)
        elif action == "move":
            event = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved, point, cg_button
            )
        else:
            return
        self._post(event)

    def scroll(self, delta_x: float, delta_y: float, phase: str = "move") -> None:
        """Continuous, phased scrolling - what a trackpad produces."""
        if not self.enabled:
            return

        dx = int(max(min(delta_x, 10000), -10000))
        dy = int(max(min(delta_y, 10000), -10000))

        if dx == 0 and dy == 0 and phase == "move":
            return

        event = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitPixel, 2, dy, dx
        )
        Quartz.CGEventSetIntegerValueField(
            event, Quartz.kCGScrollWheelEventIsContinuous, 1
        )
        Quartz.CGEventSetIntegerValueField(
            event,
            Quartz.kCGScrollWheelEventScrollPhase,
            SCROLL_PHASES.get(phase, Quartz.kCGScrollPhaseChanged),
        )
        self._post(event)

    def key(self, code: str, down: bool) -> bool:
        """Press/release by physical key code. Returns False if unmapped."""
        if not self.enabled:
            return False
        keycode = KEY_CODES.get(code)
        if keycode is None:
            return False
        if code in MODIFIER_FLAGS:
            if down:
                self._held_modifiers.add(code)
            else:
                self._held_modifiers.discard(code)
        event = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
        self._post(event)
        return True

    def text(self, value: str) -> None:
        """Type literal text."""
        if not self.enabled or not value:
            return
        for character in value:
            if self._held_modifiers:
                code = CHAR_TO_CODE.get(character)
                if code is not None and self.key(code, True):
                    self.key(code, False)
                    continue
                logger.debug("Ignoring %r: no keycode for a modifier chord", character)
                continue
            for is_down in (True, False):
                event = Quartz.CGEventCreateKeyboardEvent(None, 0, is_down)
                Quartz.CGEventKeyboardSetUnicodeString(event, 1, character)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    async def gesture(self, name: str) -> bool:
        """Mission Control and friends, via their keyboard equivalents."""
        codes = GESTURE_CHORDS.get(name)
        if codes is None:
            logger.warning("Ignoring unknown gesture %r", name)
            return False
        return await self.chord(codes)

    async def hold_chord(self, codes: list[str]) -> bool:
        """Press a chord and leave the modifiers down."""
        if not self.enabled or not codes:
            return False
        if any(code not in KEY_CODES for code in codes):
            logger.warning("Ignoring chord with an unknown key: %s", codes)
            return False

        modifiers = [c for c in codes if c in MODIFIER_FLAGS]
        keys = [c for c in codes if c not in MODIFIER_FLAGS]
        for code in modifiers:
            self.key(code, True)
            await asyncio.sleep(CHORD_STEP_SECONDS)
        await asyncio.sleep(CHORD_STEP_SECONDS)
        for code in keys:
            self.key(code, True)
            await asyncio.sleep(CHORD_STEP_SECONDS)
            self.key(code, False)
        return True

    async def chord(self, codes: list[str]) -> bool:
        """Press keys in order, hold, then release in reverse."""
        if not self.enabled or not codes:
            return False
        if any(code not in KEY_CODES for code in codes):
            logger.warning("Ignoring chord with an unknown key: %s", codes)
            return False

        pressed: list[str] = []
        try:
            for code in codes:
                self.key(code, True)
                pressed.append(code)
                await asyncio.sleep(CHORD_STEP_SECONDS)
            await asyncio.sleep(CHORD_HOLD_SECONDS)
        finally:
            for code in reversed(pressed):
                self.key(code, False)
                await asyncio.sleep(CHORD_STEP_SECONDS)
        return True

    def release_all(self) -> None:
        """Drop everything the viewer was holding."""
        for button, (px, py) in list(self._held_buttons.items()):
            spec = _BUTTONS.get(button)
            if spec is None:
                continue
            cg_button, _down, up, _dragged = spec
            Quartz.CGEventPost(
                Quartz.kCGHIDEventTap,
                Quartz.CGEventCreateMouseEvent(
                    None, up, Quartz.CGPointMake(px, py), cg_button
                ),
            )
        self._held_buttons.clear()

        for code in list(self._held_modifiers):
            keycode = KEY_CODES.get(code)
            if keycode is not None:
                Quartz.CGEventPost(
                    Quartz.kCGHIDEventTap,
                    Quartz.CGEventCreateKeyboardEvent(None, keycode, False),
                )
        self._held_modifiers.clear()

    @property
    def held_buttons(self) -> frozenset[str]:
        return frozenset(self._held_buttons)
