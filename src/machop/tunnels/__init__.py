"""Tunnel backends."""

import functools
import shutil
from typing import Callable

from .base import Tunnel, TunnelError
from .localhost_run import LocalhostRunTunnel
from .supervisor import TunnelSupervisor

BACKENDS = ("auto", "localhost.run", "cloudflared", "ngrok", "cloudflare-named")


def resolve_backend(kind: str) -> str:
    """Turn `auto` into a concrete backend name. Other names pass through."""
    if kind != "auto":
        return kind
    if shutil.which("cloudflared") is not None:
        return "cloudflared"
    return "localhost.run"


def create_tunnel(
    kind: str,
    domain: str | None = None,
    cf_tunnel: str | None = None,
    cf_hostname: str | None = None,
) -> Callable[[], Tunnel]:
    """Return a zero-argument factory for the named backend."""
    resolved = resolve_backend(kind)
    if resolved == "localhost.run":
        return LocalhostRunTunnel
    if resolved == "cloudflared":
        from .cloudflared import CloudflaredTunnel

        return CloudflaredTunnel
    if resolved == "cloudflare-named":
        from .cloudflared import NamedCloudflaredTunnel

        return functools.partial(
            NamedCloudflaredTunnel, tunnel=cf_tunnel, hostname=cf_hostname
        )
    if resolved == "ngrok":
        from .ngrok import NgrokTunnel

        return functools.partial(NgrokTunnel, domain=domain)
    raise ValueError(f"Unknown tunnel backend {kind!r}; expected one of {BACKENDS}")


__all__ = [
    "BACKENDS",
    "LocalhostRunTunnel",
    "Tunnel",
    "TunnelError",
    "TunnelSupervisor",
    "create_tunnel",
    "resolve_backend",
]
