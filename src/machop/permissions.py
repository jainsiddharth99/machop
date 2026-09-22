"""macOS TCC permission checks."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PermissionStatus:
    screen_recording: bool
    accessibility: bool

    @property
    def ok(self) -> bool:
        return self.screen_recording and self.accessibility


def check_screen_recording(prompt: bool = True) -> bool:
    if sys.platform != "darwin":
        return True
    try:
        import Quartz
    except ImportError:
        logger.warning("Quartz unavailable; cannot verify Screen Recording access")
        return False
    if not hasattr(Quartz, "CGPreflightScreenCaptureAccess"):
        return True
    granted = bool(Quartz.CGPreflightScreenCaptureAccess())
    if not granted and prompt and hasattr(Quartz, "CGRequestScreenCaptureAccess"):
        Quartz.CGRequestScreenCaptureAccess()
        granted = bool(Quartz.CGPreflightScreenCaptureAccess())
    return granted


def check_accessibility(prompt: bool = True) -> bool:
    if sys.platform != "darwin":
        return True
    try:
        import ApplicationServices
    except ImportError:
        logger.warning("ApplicationServices unavailable; cannot verify Accessibility")
        return False
    if hasattr(ApplicationServices, "AXIsProcessTrustedWithOptions"):
        options = {"AXTrustedCheckOptionPrompt": bool(prompt)}
        return bool(ApplicationServices.AXIsProcessTrustedWithOptions(options))
    if hasattr(ApplicationServices, "AXIsProcessTrusted"):
        return bool(ApplicationServices.AXIsProcessTrusted())
    return True


def check_permissions(prompt: bool = True) -> PermissionStatus:
    return PermissionStatus(
        screen_recording=check_screen_recording(prompt),
        accessibility=check_accessibility(prompt),
    )


def describe_missing(status: PermissionStatus) -> str:
    lines = ["", "Machop needs two macOS permissions before it can start.", ""]
    if not status.screen_recording:
        lines += [
            "  Screen Recording  - to see the display",
            "    System Settings > Privacy & Security > Screen & System Audio Recording",
            "",
        ]
    if not status.accessibility:
        lines += [
            "  Accessibility     - to move the pointer and type",
            "    System Settings > Privacy & Security > Accessibility",
            "",
        ]
    lines += [
        "Grant these to the app running this command (Terminal, iTerm, your IDE),",
        "then QUIT AND REOPEN that app - macOS does not apply either permission",
        "to a process that is already running.",
        "",
    ]
    return "\n".join(lines)
