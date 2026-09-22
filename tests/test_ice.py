"""ICE interface selection."""

import time

import pytest

from machop.ice import (
    limit_ice_interfaces,
    local_addresses,
    routable_addresses,
    stun_reachable,
)

STUN = "stun:stun.l.google.com:19302"


async def test_unroutable_address_is_not_reachable():
    assert await stun_reachable("stun:192.0.2.1:19302", timeout=0.5) is False


async def test_malformed_url_is_not_reachable():
    assert await stun_reachable("not-a-stun-url", timeout=0.5) is False


async def test_empty_host_is_not_reachable():
    assert await stun_reachable("stun:", timeout=0.5) is False


async def test_probe_returns_quickly_when_blocked():
    """The whole point is not waiting 5s."""
    started = time.monotonic()
    await stun_reachable("stun:192.0.2.1:19302", timeout=0.5)
    assert time.monotonic() - started < 2.0


def test_local_addresses_sees_this_machine():
    addresses = local_addresses()
    assert addresses, "aioice must report at least one local address"


def test_aioice_never_offers_loopback():
    """Documents why nothing here special-cases 127.0.0.1: aioice filters it out, so same-machine connections already use the LAN address."""
    assert "127.0.0.1" not in local_addresses()


def test_aioice_patch_point_still_exists():
    """limit_ice_interfaces monkeypatches this; an aioice upgrade that moves or renames it must fail here rather than silently do nothing."""
    import aioice.ice

    assert callable(getattr(aioice.ice, "get_host_addresses", None))


def test_limiting_interfaces_takes_effect():
    import aioice.ice

    original = aioice.ice.get_host_addresses
    try:
        limit_ice_interfaces(["10.0.0.1"])
        assert aioice.ice.get_host_addresses(True, False) == ["10.0.0.1"]
    finally:
        aioice.ice.get_host_addresses = original


@pytest.mark.network
async def test_routable_addresses_drops_dead_interfaces():
    addresses = await routable_addresses(STUN, timeout=1.5)
    assert addresses, "must never return nothing, or ICE has no candidates"
    assert len(addresses) <= len(local_addresses())


@pytest.mark.network
async def test_google_stun_is_reachable():
    assert await stun_reachable(STUN, timeout=3.0) is True
