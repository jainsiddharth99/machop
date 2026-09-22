"""The Mac's pasteboard, for copy and paste between it and the viewer."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MAX_CLIPBOARD_CHARS = 64_000


def _pasteboard():
    from AppKit import NSPasteboard

    return NSPasteboard.generalPasteboard()


def read_text() -> str | None:
    """The clipboard's text, or None if it holds something else (or nothing)."""
    try:
        from AppKit import NSPasteboardTypeString

        value = _pasteboard().stringForType_(NSPasteboardTypeString)
    except Exception:
        logger.debug("Could not read the pasteboard", exc_info=True)
        return None
    if value is None:
        return None
    text = str(value)
    if len(text) > MAX_CLIPBOARD_CHARS:
        logger.info(
            "Truncating a %d character clipboard to %d",
            len(text),
            MAX_CLIPBOARD_CHARS,
        )
        text = text[:MAX_CLIPBOARD_CHARS]
    return text


def write_text(value: str) -> bool:
    """Replace the clipboard's contents. Returns False if it did not take."""
    if not isinstance(value, str) or len(value) > MAX_CLIPBOARD_CHARS:
        return False
    try:
        from AppKit import NSPasteboardTypeString

        board = _pasteboard()
        board.clearContents()
        return bool(board.setString_forType_(value, NSPasteboardTypeString))
    except Exception:
        logger.debug("Could not write the pasteboard", exc_info=True)
        return False
