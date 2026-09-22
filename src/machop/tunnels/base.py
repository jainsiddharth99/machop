"""Shared tunnel interface."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod


class TunnelError(RuntimeError):
    """The tunnel could not be established or was lost."""


class Tunnel(ABC):
    name: str = "tunnel"

    def __init__(self) -> None:
        self.public_url: str | None = None
        self.lost = asyncio.Event()

    @abstractmethod
    async def start(self, local_port: int) -> str:
        """Open the tunnel and return the public URL."""

    @abstractmethod
    async def stop(self) -> None:
        """Terminate and reap. Must never leave an orphan process."""

    async def __aenter__(self) -> "Tunnel":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()
