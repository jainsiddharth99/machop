"""ICE interface selection."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct

logger = logging.getLogger(__name__)

_BINDING_REQUEST = 0x0001
_BINDING_RESPONSE = 0x0101
_MAGIC_COOKIE = 0x2112A442

def _parse(url: str) -> tuple[str, int] | None:
    """`stun:host:port` -> (host, port). Port defaults to 3478."""
    if not url.startswith("stun:"):
        return None
    remainder = url[len("stun:") :]
    if not remainder:
        return None
    host, separator, port = remainder.rpartition(":")
    if not separator:
        return (remainder, 3478)
    if not host or not port.isdigit():
        return None
    return (host, int(port))


async def stun_reachable(
    url: str, timeout: float = 1.0, local_address: str | None = None
) -> bool:
    """True if the server answers a Binding Request within `timeout`."""
    parsed = _parse(url)
    if parsed is None:
        logger.debug("Not a STUN url: %s", url)
        return False
    host, port = parsed

    transaction_id = os.urandom(12)
    request = struct.pack(">HHI", _BINDING_REQUEST, 0, _MAGIC_COOKIE) + transaction_id

    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        if local_address is not None:
            sock.bind((local_address, 0))
        await asyncio.wait_for(loop.sock_connect(sock, (host, port)), timeout=timeout)
        await loop.sock_sendall(sock, request)
        data = await asyncio.wait_for(loop.sock_recv(sock, 1024), timeout=timeout)
    except (OSError, asyncio.TimeoutError, socket.gaierror):
        return False
    finally:
        sock.close()

    if len(data) < 20:
        return False
    message_type, _length, cookie = struct.unpack(">HHI", data[:8])
    return (
        message_type == _BINDING_RESPONSE
        and cookie == _MAGIC_COOKIE
        and data[8:20] == transaction_id
    )


def local_addresses() -> list[str]:
    """Every IPv4 address aioice would gather from."""
    from aioice.ice import get_host_addresses

    return list(get_host_addresses(use_ipv4=True, use_ipv6=False))


async def routable_addresses(stun_url: str, timeout: float = 1.0) -> list[str]:
    """Local addresses that can actually reach the STUN server."""
    addresses = local_addresses()
    if not addresses:
        return addresses

    results = await asyncio.gather(
        *(stun_reachable(stun_url, timeout, local_address=a) for a in addresses),
        return_exceptions=True,
    )
    usable = [a for a, ok in zip(addresses, results) if ok is True]

    if not usable:
        logger.warning(
            "No local interface could reach %s; peer-to-peer will not work and "
            "the encrypted relay will be used instead",
            stun_url,
        )
        return addresses

    dropped = len(addresses) - len(usable)
    if dropped:
        logger.info(
            "Ignoring %d local interface(s) that cannot reach the internet; "
            "this avoids a 5s stall on every connection",
            dropped,
        )
    return usable


def limit_ice_interfaces(addresses: list[str]) -> None:
    """Restrict aioice to `addresses`."""
    import aioice.ice

    kept = list(addresses)

    def _fixed(use_ipv4: bool, use_ipv6: bool) -> list[str]:
        return list(kept)

    aioice.ice.get_host_addresses = _fixed
    logger.debug("ICE limited to %s", ", ".join(kept))
