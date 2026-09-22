import asyncio

import pytest

from machop.tunnels.base import TunnelError
from machop.tunnels.localhost_run import (
    IGNORED_HOSTS,
    URL_PATTERN,
    LocalhostRunTunnel as Tunnel,
)


def _extract(line: str) -> str | None:
    match = URL_PATTERN.search(line)
    if match and not any(host in line for host in IGNORED_HOSTS):
        return f"https://{match.group(1)}"
    return None


def test_extracts_the_forwarding_url():
    line = "8b843c60c7e73e.lhr.life tunneled with tls termination, https://8b843c60c7e73e.lhr.life"
    assert _extract(line) == "https://8b843c60c7e73e.lhr.life"


def test_ignores_the_admin_dashboard_url():
    assert _extract("To manage custom domains go to https://admin.localhost.run/") is None


def test_ignores_banner_noise():
    for line in ("Welcome to localhost.run!", "** your connection id is 1.2.3.4:5 **",
                 "authn: authenticated as anonymous user"):
        assert _extract(line) is None


async def test_double_start_is_rejected():
    tunnel = Tunnel()
    tunnel._process = object()
    with pytest.raises(TunnelError, match="already running"):
        await tunnel.start(local_port=1234)
    tunnel._process = None


async def test_stop_is_safe_when_never_started():
    await Tunnel().stop()


async def test_stop_terminates_and_reaps_the_process():
    """v0.1 called terminate() without ever awaiting the child, which left public tunnels alive after the tool exited."""
    tunnel = Tunnel()
    tunnel._process = await asyncio.create_subprocess_exec(
        "sleep", "300", stdout=asyncio.subprocess.DEVNULL
    )
    process = tunnel._process
    await tunnel.stop()
    assert process.returncode is not None, "child process was not reaped"
    assert tunnel._process is None


from machop.tunnels.cloudflared import URL_PATTERN as CF_PATTERN


def test_cloudflared_url_is_extracted():
    line = (
        "2026-09-18T10:00:00Z INF |  "
        "https://spare-brief-glass-tent.trycloudflare.com  |"
    )
    match = CF_PATTERN.search(line)
    assert match and match.group(1) == "spare-brief-glass-tent.trycloudflare.com"


def test_cloudflared_ignores_the_announcement_line():
    assert CF_PATTERN.search("INF Requesting new quick Tunnel on trycloudflare.com...") is None


from machop.tunnels import BACKENDS, create_tunnel


def test_every_advertised_backend_can_be_constructed():
    """A backend in --tunnel's choices that cannot be imported would only fail at runtime, after capture has already started."""
    for kind in BACKENDS:
        assert callable(create_tunnel(kind))


def test_auto_prefers_cloudflared_when_installed(monkeypatch):
    from machop.tunnels import resolve_backend

    monkeypatch.setattr("machop.tunnels.shutil.which", lambda name: "/usr/bin/" + name)
    assert resolve_backend("auto") == "cloudflared"


def test_auto_falls_back_to_localhost_run(monkeypatch):
    """Anyone without cloudflared must still get a working zero-setup tunnel."""
    from machop.tunnels import resolve_backend

    monkeypatch.setattr("machop.tunnels.shutil.which", lambda name: None)
    assert resolve_backend("auto") == "localhost.run"


def test_explicit_backend_is_not_overridden(monkeypatch):
    from machop.tunnels import resolve_backend

    monkeypatch.setattr("machop.tunnels.shutil.which", lambda name: "/usr/bin/" + name)
    assert resolve_backend("localhost.run") == "localhost.run"


def test_tailscale_backend_is_gone():
    """Removed deliberately: it was the one backend never verified end to end, it needs an account, and its 100.64.0.0/10 range collides with NetBird."""
    assert "tailscale" not in BACKENDS
    with pytest.raises(ValueError):
        create_tunnel("tailscale")


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="Unknown tunnel backend"):
        create_tunnel("nope")


def test_cloudflared_readiness_line_is_recognised():
    """cloudflared prints the URL ~1s before the tunnel actually works ("it may take some time to be reachable")."""
    from machop.tunnels.cloudflared import READY_PATTERN

    real = (
        "2026-09-20T09:12:19Z INF Registered tunnel connection connIndex=0 "
        "connection=c9d955b8-d544-46f2-a390-21feccde6e65 ip=198.41.200.43 location=del05"
    )
    assert READY_PATTERN.search(real)
    assert READY_PATTERN.search("INF Requesting new quick Tunnel on trycloudflare.com...") is None


import os
import stat
import textwrap

from machop.tunnels import BACKENDS, create_tunnel, resolve_backend
from machop.tunnels.cloudflared import NamedCloudflaredTunnel, _bare_host


def test_a_bare_hostname_is_left_alone():
    assert _bare_host("mac.example.com") == "mac.example.com"


def test_a_scheme_is_stripped():
    assert _bare_host("https://mac.example.com") == "mac.example.com"
    assert _bare_host("http://mac.example.com/") == "mac.example.com"


def test_stripping_does_not_eat_leading_letters():
    """lstrip('https://') removes CHARACTERS, so it would maul any hostname starting with h, t, p, s, : or / - `shop.example.com` becomes `op.example.com`."""
    assert _bare_host("shop.example.com") == "shop.example.com"
    assert _bare_host("https://ssl.test.dev") == "ssl.test.dev"
    assert _bare_host("http.example.com") == "http.example.com"


def test_it_is_a_selectable_backend():
    assert "cloudflare-named" in BACKENDS
    assert resolve_backend("cloudflare-named") == "cloudflare-named"


def test_auto_never_picks_it():
    """It needs a domain and a one-off login; choosing it silently would be a dead end."""
    assert resolve_backend("auto") in ("cloudflared", "localhost.run")


def test_the_factory_carries_both_settings_across_reconnects():
    factory = create_tunnel(
        "cloudflare-named", cf_tunnel="machop", cf_hostname="mac.example.com"
    )
    for _ in range(2):
        built = factory()
        assert built.tunnel == "machop"
        assert built.hostname == "mac.example.com"


async def test_a_missing_hostname_is_refused_up_front():
    """Otherwise it starts, registers, and then has no URL to hand back."""
    tunnel = NamedCloudflaredTunnel(tunnel="machop", hostname=None)
    with pytest.raises(TunnelError, match="cloudflare-hostname"):
        await tunnel.start(local_port=8765)


async def test_a_missing_tunnel_name_is_refused_up_front():
    tunnel = NamedCloudflaredTunnel(tunnel=None, hostname="mac.example.com")
    with pytest.raises(TunnelError, match="cloudflare-tunnel"):
        await tunnel.start(local_port=8765)


def _fake_cloudflared(tmp_path, script: str):
    path = tmp_path / "cloudflared"
    path.write_text("#!/bin/sh\n" + textwrap.dedent(script))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    os.environ["PATH"] = f"{tmp_path}{os.pathsep}{os.environ['PATH']}"
    return path


@pytest.fixture
def restore_path():
    original = os.environ["PATH"]
    yield
    os.environ["PATH"] = original


async def test_it_returns_the_routed_hostname_not_a_parsed_url(tmp_path, restore_path):
    """A named tunnel never prints its hostname - that lives in the DNS record - so it has to come from the flag."""
    marker = tmp_path / "argv"
    _fake_cloudflared(tmp_path, f"""
        echo "$@" > {marker}
        echo 'INF Registered tunnel connection connIndex=0'
        sleep 30
    """)
    tunnel = NamedCloudflaredTunnel(tunnel="machop", hostname="mac.example.com")
    try:
        url = await asyncio.wait_for(tunnel.start(local_port=8765), timeout=20)
        assert url == "https://mac.example.com"
    finally:
        await tunnel.stop()
    argv = marker.read_text()
    assert "run" in argv and "machop" in argv
    assert "http://127.0.0.1:8765" in argv


async def test_a_missing_tunnel_says_how_to_check(tmp_path, restore_path):
    _fake_cloudflared(tmp_path, "exit 1\n")
    tunnel = NamedCloudflaredTunnel(tunnel="nope", hostname="mac.example.com")
    with pytest.raises(TunnelError, match="cloudflared tunnel list"):
        await asyncio.wait_for(tunnel.start(local_port=8765), timeout=20)


async def test_losing_the_process_sets_the_lost_event(tmp_path, restore_path):
    _fake_cloudflared(tmp_path, """
        echo 'INF Registered tunnel connection connIndex=0'
        sleep 0.3
    """)
    tunnel = NamedCloudflaredTunnel(tunnel="machop", hostname="mac.example.com")
    try:
        await asyncio.wait_for(tunnel.start(local_port=8765), timeout=20)
        await asyncio.wait_for(tunnel.lost.wait(), timeout=10)
    finally:
        await tunnel.stop()
