"""Shared tunnel interface."""

from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod
from typing import Callable

OnUrl = Callable[[str], None]


class TunnelError(RuntimeError):
    """The tunnel could not be established or was lost."""


class Tunnel(ABC):
    name: str = "tunnel"

    def __init__(self) -> None:
        self.public_url: str | None = None
        self.lost = asyncio.Event()

    @abstractmethod
    async def start(self, local_port: int, on_url: OnUrl | None = None) -> str:
        """Open the tunnel and return the public URL once it answers.

        `on_url` fires earlier, as soon as the address is known but before it
        is confirmed to work. A quick Cloudflare tunnel spends around ten
        seconds waiting for DNS to publish, and there is no reason to make
        someone stare at a blank terminal for it.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Terminate and reap. Must never leave an orphan process."""

    def kill_now(self) -> None:
        """Best-effort synchronous kill, for a forced exit.

        Called from a signal handler on the way to os._exit, where there is
        no event loop left to await stop(). An orphaned tunnel child keeps a
        public address pointing at a server that no longer exists.
        """
        process = getattr(self, "_process", None)
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(Exception):
            process.kill()

    async def __aenter__(self) -> "Tunnel":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()
