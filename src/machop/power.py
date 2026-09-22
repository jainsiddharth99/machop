"""macOS power assertions."""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import subprocess
from ctypes import POINTER, byref, c_char_p, c_int, c_uint32, c_void_p

logger = logging.getLogger(__name__)

ASSERTION_NAME_PREFIX = "machop"

_ASSERTION_TYPES = {
    "system": "PreventUserIdleSystemSleep",
    "display": "PreventUserIdleDisplaySleep",
}
_LEVEL_ON = 255
_SUCCESS = 0
_UTF8 = 0x08000100
_USER_ACTIVE_LOCAL = 0


def _load() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    iokit = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
    core = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))

    core.CFStringCreateWithCString.restype = c_void_p
    core.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
    core.CFRelease.argtypes = [c_void_p]

    iokit.IOPMAssertionCreateWithName.argtypes = [
        c_void_p,
        c_uint32,
        c_void_p,
        POINTER(c_uint32),
    ]
    iokit.IOPMAssertionCreateWithName.restype = c_int
    iokit.IOPMAssertionRelease.argtypes = [c_uint32]
    iokit.IOPMAssertionRelease.restype = c_int
    iokit.IOPMAssertionDeclareUserActivity.argtypes = [
        c_void_p,
        c_int,
        POINTER(c_uint32),
    ]
    iokit.IOPMAssertionDeclareUserActivity.restype = c_int
    return iokit, core


class PowerAssertions:
    """Holds macOS idle-sleep assertions. Not thread-safe; call from the loop."""

    def __init__(self) -> None:
        self._iokit, self._core = _load()
        self._ids: dict[str, int] = {}
        self._wake_id = c_uint32(0)


    def _cfstr(self, text: str) -> c_void_p:
        return c_void_p(
            self._core.CFStringCreateWithCString(None, text.encode("utf-8"), _UTF8)
        )

    def _hold(self, kind: str) -> None:
        if kind in self._ids:
            return
        assertion_type = self._cfstr(_ASSERTION_TYPES[kind])
        assertion_name = self._cfstr(f"{ASSERTION_NAME_PREFIX} ({kind})")
        assertion_id = c_uint32(0)
        try:
            result = self._iokit.IOPMAssertionCreateWithName(
                assertion_type, _LEVEL_ON, assertion_name, byref(assertion_id)
            )
        finally:
            self._core.CFRelease(assertion_type)
            self._core.CFRelease(assertion_name)

        if result != _SUCCESS:
            logger.warning(
                "Could not prevent %s sleep (IOKit error %s); "
                "this Mac may sleep and stop responding",
                kind,
                result,
            )
            return
        self._ids[kind] = assertion_id.value
        logger.debug("Holding %s sleep assertion", kind)

    def _release(self, kind: str) -> None:
        assertion_id = self._ids.pop(kind, None)
        if assertion_id is None:
            return
        if self._iokit.IOPMAssertionRelease(assertion_id) != _SUCCESS:
            logger.warning("Failed to release %s sleep assertion", kind)


    def hold_system(self) -> None:
        self._hold("system")

    def release_system(self) -> None:
        self._release("system")

    def hold_display(self) -> None:
        self._hold("display")

    def release_display(self) -> None:
        self._release("display")
        self._release_wake()

    def _release_wake(self) -> None:
        if self._wake_id.value:
            self._iokit.IOPMAssertionRelease(self._wake_id)
            self._wake_id = c_uint32(0)

    def release_all(self) -> None:
        for kind in list(self._ids):
            self._release(kind)
        self._release_wake()

    def wake_display(self) -> bool:
        """Wake the panel."""
        name = self._cfstr(f"{ASSERTION_NAME_PREFIX} wake")
        try:
            result = self._iokit.IOPMAssertionDeclareUserActivity(
                name, _USER_ACTIVE_LOCAL, byref(self._wake_id)
            )
        finally:
            self._core.CFRelease(name)
        if result != _SUCCESS:
            logger.warning("Could not wake the display (IOKit error %s)", result)
            return False
        return True

    @property
    def held(self) -> frozenset[str]:
        return frozenset(self._ids)

    def __enter__(self) -> "PowerAssertions":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release_all()


def on_battery() -> bool:
    """True when running on battery. `pmset -g batt` names the active source."""
    try:
        output = subprocess.run(
            ["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "Battery Power" in output


def startup_warnings() -> list[str]:
    """Limitations that cannot be engineered around (design doc §9.1)."""
    warnings = [
        "While someone is connected your screen is lit and readable by anyone "
        "near this Mac.",
    ]
    if on_battery():
        warnings.append(
            "This Mac is on battery. Closing the lid will sleep it and end the "
            "session - no software can prevent that. Leave it plugged in and open."
        )
    return warnings
