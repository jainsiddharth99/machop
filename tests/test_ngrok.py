"""ngrok is the only backend that can hold a URL steady across restarts."""

import asyncio
import os
import stat
import textwrap

import pytest

from machop.tunnels import BACKENDS, create_tunnel, resolve_backend
from machop.tunnels.base import TunnelError
from machop.tunnels.ngrok import NgrokTunnel, _failure, _parse

STARTED = (
    '{"addr":"http://localhost:8765","lvl":"info","msg":"started tunnel",'
    '"name":"command_line","obj":"tunnels","url":"https://kite.ngrok-free.app"}'
)


def _fake_ngrok(tmp_path, script: str):
    """Put an executable called `ngrok` at the front of PATH."""
    path = tmp_path / "ngrok"
    path.write_text("#!/bin/sh\n" + textwrap.dedent(script))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    os.environ["PATH"] = f"{tmp_path}{os.pathsep}{os.environ['PATH']}"
    return path


@pytest.fixture
def restore_path():
    original = os.environ["PATH"]
    yield
    os.environ["PATH"] = original


def test_parses_the_started_tunnel_url():
    record = _parse(STARTED.encode())
    assert record["url"] == "https://kite.ngrok-free.app"


def test_ignores_the_non_json_preamble():
    assert _parse(b"ngrok (Ctrl+C to quit)\n") is None
    assert _parse(b"") is None
    assert _parse(b"{not json}") is None


def test_info_records_are_not_failures():
    assert _failure(_parse(STARTED.encode())) is None


def test_a_missing_authtoken_says_how_to_fix_it():
    record = {"lvl": "eror", "msg": "failed to start tunnel",
              "err": "ERR_NGROK_4018: authentication failed"}
    assert "add-authtoken" in _failure(record)


def test_an_unclaimed_domain_says_how_to_fix_it():
    record = {"lvl": "eror", "msg": "ERR_NGROK_313 domain not found"}
    assert "dashboard.ngrok.com/domains" in _failure(record)


def test_an_unrecognised_error_is_still_reported():
    message = _failure({"lvl": "eror", "err": "the sky fell in"})
    assert "the sky fell in" in message


def test_a_bare_domain_gains_a_scheme():
    assert NgrokTunnel._normalise_domain("kite.ngrok-free.app") == "https://kite.ngrok-free.app"


def test_a_full_url_is_left_alone():
    assert NgrokTunnel._normalise_domain("https://kite.ngrok-free.app") == (
        "https://kite.ngrok-free.app"
    )


def test_ngrok_is_a_selectable_backend():
    assert "ngrok" in BACKENDS
    assert resolve_backend("ngrok") == "ngrok"


def test_auto_never_picks_ngrok():
    """It needs an account; choosing it silently would be a dead end."""
    assert resolve_backend("auto") in ("cloudflared", "localhost.run")


def test_the_factory_carries_the_domain_across_reconnects():
    """The supervisor rebuilds the tunnel on every reconnect, so a domain passed once must survive into every later instance."""
    factory = create_tunnel("ngrok", domain="kite.ngrok-free.app")
    assert factory().domain == "kite.ngrok-free.app"
    assert factory().domain == "kite.ngrok-free.app"


def test_other_backends_ignore_the_domain():
    assert create_tunnel("localhost.run", domain="kite.ngrok-free.app") is not None


async def test_start_returns_the_url_from_the_log(tmp_path, restore_path):
    _fake_ngrok(tmp_path, f"""
        echo 'ngrok (Ctrl+C to quit)'
        echo '{STARTED}'
        sleep 30
    """)
    tunnel = NgrokTunnel()
    try:
        url = await asyncio.wait_for(tunnel.start(local_port=8765), timeout=20)
        assert url == "https://kite.ngrok-free.app"
        assert tunnel.public_url == url
    finally:
        await tunnel.stop()


async def test_the_reserved_domain_is_passed_to_ngrok(tmp_path, restore_path):
    marker = tmp_path / "argv"
    _fake_ngrok(tmp_path, f"""
        echo "$@" > {marker}
        echo '{STARTED}'
        sleep 30
    """)
    tunnel = NgrokTunnel(domain="kite.ngrok-free.app")
    try:
        await asyncio.wait_for(tunnel.start(local_port=8765), timeout=20)
    finally:
        await tunnel.stop()
    assert "--url https://kite.ngrok-free.app" in marker.read_text()


async def test_a_setup_error_is_raised_not_waited_out(tmp_path, restore_path):
    """Without this the user just watches a 45s timeout and learns nothing."""
    _fake_ngrok(tmp_path, """
        echo '{"lvl":"eror","msg":"failed","err":"ERR_NGROK_4018 no authtoken"}'
        sleep 30
    """)
    tunnel = NgrokTunnel()
    with pytest.raises(TunnelError, match="add-authtoken"):
        await asyncio.wait_for(tunnel.start(local_port=8765), timeout=20)
    assert tunnel._process is None, "the failed child must be reaped"


async def test_an_early_exit_is_reported(tmp_path, restore_path):
    _fake_ngrok(tmp_path, "exit 3\n")
    tunnel = NgrokTunnel()
    with pytest.raises(TunnelError, match="status 3"):
        await asyncio.wait_for(tunnel.start(local_port=8765), timeout=20)
    assert tunnel._process is None, "the exited child must be cleaned up"
    await tunnel.stop()


async def test_a_missing_binary_explains_the_alternative(tmp_path, restore_path):
    os.environ["PATH"] = str(tmp_path)
    with pytest.raises(TunnelError, match="brew install ngrok"):
        await NgrokTunnel().start(local_port=8765)


async def test_stop_reaps_the_child(tmp_path, restore_path):
    _fake_ngrok(tmp_path, f"""
        echo '{STARTED}'
        sleep 300
    """)
    tunnel = NgrokTunnel()
    await asyncio.wait_for(tunnel.start(local_port=8765), timeout=20)
    process = tunnel._process
    await tunnel.stop()
    assert process.returncode is not None, "child process was not reaped"
    assert tunnel._process is None
    assert tunnel.public_url is None


async def test_stop_is_safe_when_never_started():
    await NgrokTunnel().stop()


async def test_losing_the_process_sets_the_lost_event(tmp_path, restore_path):
    """The supervisor waits on `lost` to know it must reconnect."""
    _fake_ngrok(tmp_path, f"""
        echo '{STARTED}'
        sleep 0.3
    """)
    tunnel = NgrokTunnel()
    try:
        await asyncio.wait_for(tunnel.start(local_port=8765), timeout=20)
        await asyncio.wait_for(tunnel.lost.wait(), timeout=10)
    finally:
        await tunnel.stop()


def _parsed(argv, monkeypatch):
    """Run main() far enough to see the final args, without starting anything."""
    from machop import cli

    captured = {}

    async def fake_run(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr(cli, "run", fake_run)
    assert cli.main(argv) == 0
    return captured["args"]


@pytest.mark.macos
def test_ngrok_domain_implies_the_ngrok_backend(monkeypatch):
    """Making the user also remember --tunnel ngrok is just a way to get a confusing error from whichever backend `auto` happened to pick."""
    args = _parsed(["--ngrok-domain", "kite.ngrok-free.app"], monkeypatch)
    assert args.tunnel == "ngrok"
    assert args.ngrok_domain == "kite.ngrok-free.app"


@pytest.mark.macos
def test_an_explicit_backend_still_wins(monkeypatch):
    args = _parsed(
        ["--ngrok-domain", "kite.ngrok-free.app", "--tunnel", "cloudflared"], monkeypatch
    )
    assert args.tunnel == "cloudflared"


@pytest.mark.macos
def test_no_domain_leaves_the_backend_on_auto(monkeypatch):
    args = _parsed([], monkeypatch)
    assert args.tunnel == "auto"
    assert args.ngrok_domain is None


@pytest.mark.macos
def test_cloudflare_tunnel_implies_its_backend(monkeypatch):
    args = _parsed(["--cloudflare-tunnel", "machop",
                    "--cloudflare-hostname", "mac.example.com"], monkeypatch)
    assert args.tunnel == "cloudflare-named"
    assert args.cloudflare_hostname == "mac.example.com"


@pytest.mark.macos
def test_an_explicit_backend_beats_the_cloudflare_implication(monkeypatch):
    args = _parsed(["--cloudflare-tunnel", "machop",
                    "--tunnel", "cloudflared"], monkeypatch)
    assert args.tunnel == "cloudflared"
