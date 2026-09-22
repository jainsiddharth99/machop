"""Command line surface: the flags, and the address the user is handed."""

import pytest

pytest.importorskip("Quartz", reason="macOS only")

from machop.cli import build_parser, viewer_url


def parse(*argv):
    return build_parser().parse_args(list(argv))


def test_the_defaults_try_direct_first_and_share_no_sound():
    args = parse()
    assert args.prefer == "auto"
    assert args.no_audio is False
    assert args.no_relay is False


def test_prefer_relay_is_carried_in_the_address():
    """A query parameter rather than server state, so the SAME running Mac is reachable both ways: the plain URL goes direct on wifi, the printed one skips straight to the relay on mobile data."""
    assert viewer_url("https://x.example/", "relay") == "https://x.example/?relay=1"


def test_an_address_that_already_has_a_query_keeps_it():
    assert viewer_url("https://x.example/?a=1", "relay") == "https://x.example/?a=1&relay=1"


def test_auto_leaves_the_address_alone():
    assert viewer_url("https://x.example/", "auto") == "https://x.example/"


def test_asking_twice_does_not_double_up():
    once = viewer_url("https://x.example/", "relay")
    assert viewer_url(once, "relay") == once


def test_prefer_only_accepts_what_the_viewer_understands():
    with pytest.raises(SystemExit):
        parse("--prefer", "carrier-pigeon")


def test_the_banner_says_what_sound_is_doing():
    """The person at the Mac should be able to see, without asking, that their machine is not quietly sharing its audio."""
    from machop.capture import DisplayGeometry
    from machop.cli import _banner

    text = _banner(
        "https://x.example/", "424242", DisplayGeometry(1470, 956), (1440, 928),
        False, "encrypted relay if a direct connection does not come up",
        "on request (the speaker button in the viewer)",
    )
    assert "Sound" in text
    assert "on request" in text
