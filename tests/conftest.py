import os

import pytest

pytest_plugins = ("pytest_asyncio",)

HARDWARE_TESTS = {
    "test_capture",
    "test_clipboard",
    "test_inputs",
    "test_integration",
    "test_power",
    "test_profiles",
    "test_relay",
    "test_sound",
    "test_takeover",
}


def pytest_configure(config):
    config.addinivalue_line("markers", "macos: requires a real macOS display and TCC grants")
    config.addinivalue_line("markers", "network: needs outbound UDP to the internet")


def pytest_collection_modifyitems(config, items):
    """Skip tests needing a real screen when running on a build machine.

    These drive ScreenCaptureKit, CGEvent and IOKit against the live display.
    A hosted runner has no logged-in graphical session and cannot be granted
    Screen Recording or Accessibility, so they are skipped there and must be
    run on a real Mac before merging.
    """
    if not os.environ.get("CI"):
        return
    skip = pytest.mark.skip(reason="needs a real display and Screen Recording permission")
    for item in items:
        if item.module.__name__.rpartition(".")[2] in HARDWARE_TESTS:
            item.add_marker(skip)
