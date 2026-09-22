"""Keeps a tunnel alive across drops."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Callable

from .base import Tunnel, TunnelError

logger = logging.getLogger(__name__)

INITIAL_BACKOFF_SECONDS = 1.0


class TunnelSupervisor:
    def __init__(
        self,
        factory: Callable[[], Tunnel],
        on_url_change: Callable[[str], None] | None = None,
        max_backoff: float = 60.0,
    ) -> None:
        self._factory = factory
        self._on_url_change = on_url_change
        self._max_backoff = max_backoff
        self._tunnel: Tunnel | None = None
        self._watcher: asyncio.Task | None = None
        self._stopping = False
        self._local_port = 0

    @property
    def public_url(self) -> str | None:
        return self._tunnel.public_url if self._tunnel is not None else None

    async def start(self, local_port: int) -> str:
        """First connection."""
        self._local_port = local_port
        self._tunnel = self._factory()
        url = await self._tunnel.start(local_port)
        self._watcher = asyncio.create_task(self._watch(), name="tunnel-watch")
        return url

    async def _watch(self) -> None:
        while not self._stopping:
            tunnel = self._tunnel
            if tunnel is None:
                return
            await tunnel.lost.wait()
            if self._stopping:
                return
            logger.warning("Tunnel lost; reconnecting")
            with contextlib.suppress(Exception):
                await tunnel.stop()
            url = await self._reconnect()
            if url is None:
                return
            if self._on_url_change is not None:
                self._on_url_change(url)

    async def _reconnect(self) -> str | None:
        backoff = INITIAL_BACKOFF_SECONDS
        while not self._stopping:
            try:
                tunnel = self._factory()
                url = await tunnel.start(self._local_port)
            except (TunnelError, OSError) as exc:
                logger.warning("Reconnect failed (%s); retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                continue
            self._tunnel = tunnel
            logger.info("Tunnel restored: %s", url)
            return url
        return None

    async def stop(self) -> None:
        self._stopping = True
        if self._watcher is not None:
            self._watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watcher
            self._watcher = None
        if self._tunnel is not None:
            await self._tunnel.stop()
            self._tunnel = None
