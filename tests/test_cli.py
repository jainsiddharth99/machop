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


def test_it_refuses_politely_off_a_mac(monkeypatch, capsys):
    """pip will happily install this on Linux; running it there should say
    so in one line rather than raising ImportError out of Quartz."""
    from machop.cli import main

    monkeypatch.setattr("machop.cli.sys.platform", "linux")
    assert main([]) == 1
    assert "only runs on macOS" in capsys.readouterr().err


def test_help_and_version_still_work_anywhere(monkeypatch):
    """Both exit through argparse before the platform check, so packaging
    tools can query them on any machine."""
    import pytest as _pytest

    from machop.cli import main

    monkeypatch.setattr("machop.cli.sys.platform", "linux")
    with _pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0


async def test_ctrl_c_abandons_a_slow_startup():
    """The bug this exists for: add_signal_handler replaces the default
    SIGINT behaviour, so Ctrl-C stops raising KeyboardInterrupt and only
    sets an event. Nothing awaited during startup noticed it, so while a
    quick tunnel waited on DNS the tool was deaf to Ctrl-C for up to three
    minutes and the terminal had to be killed."""
    import asyncio

    from machop.cli import _Interrupted, until_stopped

    stop = asyncio.Event()

    async def never():
        await asyncio.sleep(3600)

    asyncio.get_running_loop().call_later(0.01, stop.set)
    with pytest.raises(_Interrupted):
        await asyncio.wait_for(until_stopped(never(), stop), timeout=2.0)


async def test_an_abandoned_startup_does_not_leak_its_task():
    """Walking away from the tunnel is only safe if it is actually
    cancelled; a live cloudflared child outliving the process is how an
    address keeps pointing at a server that has gone."""
    import asyncio

    from machop.cli import _Interrupted, until_stopped

    stop = asyncio.Event()
    started = asyncio.Event()
    cancelled = False

    async def slow():
        nonlocal cancelled
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled = True
            raise

    task = asyncio.create_task(until_stopped(slow(), stop))
    await started.wait()
    stop.set()
    with pytest.raises(_Interrupted):
        await asyncio.wait_for(task, timeout=2.0)
    assert cancelled, "the startup task was abandoned but never cancelled"


async def test_a_startup_that_finishes_first_returns_normally():
    import asyncio

    from machop.cli import until_stopped

    stop = asyncio.Event()

    async def quick():
        return "https://example.test"

    assert await until_stopped(quick(), stop) == "https://example.test"
