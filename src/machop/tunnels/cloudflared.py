"""Cloudflare quick tunnel."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil

import aiohttp

from .base import Tunnel, TunnelError

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https://([a-z0-9][a-z0-9-]*\.trycloudflare\.com)\b", re.I)
READY_PATTERN = re.compile(r"Registered tunnel connection", re.I)
START_TIMEOUT_SECONDS = 45.0
DNS_GRACE_SECONDS = 5.0
REACHABLE_TIMEOUT_SECONDS = 60.0


class CloudflaredTunnel(Tunnel):
    name = "cloudflared"

    def __init__(self) -> None:
        super().__init__()
        self._process: asyncio.subprocess.Process | None = None
        self._drain_task: asyncio.Task | None = None

    async def start(self, local_port: int) -> str:
        if shutil.which("cloudflared") is None:
            raise TunnelError(
                "`cloudflared` is not installed. Install it with "
                "`brew install cloudflared`, or use the default "
                "--tunnel localhost.run"
            )
        self._process = await asyncio.create_subprocess_exec(
            "cloudflared",
            "tunnel",
            "--url",
            f"http://127.0.0.1:{local_port}",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            self.public_url = await asyncio.wait_for(
                self._read_until_ready(), timeout=START_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            await self.stop()
            raise TunnelError(
                "Timed out waiting for cloudflared to register its tunnel"
            ) from None
        except TunnelError:
            await self.stop()
            raise
        self._drain_task = asyncio.create_task(self._drain(), name="cloudflared-drain")
        await self._wait_until_reachable(self.public_url)
        return self.public_url

    async def _wait_until_reachable(self, url: str) -> None:
        """Poll the tunnel until it actually answers, so the printed URL works."""
        await asyncio.sleep(DNS_GRACE_SECONDS)
        deadline = asyncio.get_running_loop().time() + REACHABLE_TIMEOUT_SECONDS
        attempt = 0
        while asyncio.get_running_loop().time() < deadline:
            attempt += 1
            try:
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as http:
                    async with http.get(f"{url}/health") as response:
                        if response.status < 500:
                            logger.debug("Tunnel reachable after %d attempt(s)", attempt)
                            return
            except Exception as exc:
                logger.debug("Tunnel not reachable yet (%s)", exc)
            await asyncio.sleep(3.0)
        logger.warning(
            "Tunnel did not become reachable within %.0fs; the URL may need "
            "another moment to start working",
            REACHABLE_TIMEOUT_SECONDS,
        )

    async def _read_until_ready(self) -> str:
        """Return only once the URL is known AND the edge has registered."""
        assert self._process is not None and self._process.stdout is not None
        url: str | None = None
        while True:
            line = await self._process.stdout.readline()
            if not line:
                code = await self._process.wait()
                raise TunnelError(f"cloudflared exited with status {code}")
            text = line.decode("utf-8", "replace").strip()
            if text:
                logger.debug("cloudflared: %s", text)
            if url is None:
                match = URL_PATTERN.search(text)
                if match:
                    url = f"https://{match.group(1)}"
                    continue
            if url is not None and READY_PATTERN.search(text):
                return url

    async def _drain(self) -> None:
        """Keep the pipe empty; a full 64 KiB buffer would block the child."""
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                logger.debug("cloudflared: %s", line.decode("utf-8", "replace").strip())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("cloudflared drain ended", exc_info=True)
        finally:
            self.lost.set()

    async def stop(self) -> None:
        if self._drain_task is not None:
            self._drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._drain_task
            self._drain_task = None
        process, self._process = self._process, None
        self.public_url = None
        if process is None:
            return
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        with contextlib.suppress(Exception):
            process._transport.close()


def _bare_host(hostname: str) -> str:
    """Accept `mac.example.com` or the full https:// form alike."""
    host = hostname.strip()
    for prefix in ("https://", "http://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix):]
            break
    return host.strip("/")


class NamedCloudflaredTunnel(CloudflaredTunnel):
    """A Cloudflare *named* tunnel: permanent URL, no interstitial, no cap."""

    name = "cloudflare-named"

    def __init__(self, tunnel: str | None = None, hostname: str | None = None) -> None:
        super().__init__()
        self.tunnel = tunnel
        self.hostname = hostname

    async def start(self, local_port: int) -> str:
        if shutil.which("cloudflared") is None:
            raise TunnelError(
                "`cloudflared` is not installed. Install it with "
                "`brew install cloudflared`"
            )
        if not self.tunnel or not self.hostname:
            raise TunnelError(
                "A named tunnel needs both --cloudflare-tunnel and "
                "--cloudflare-hostname"
            )
        self._process = await asyncio.create_subprocess_exec(
            "cloudflared",
            "tunnel",
            "--no-autoupdate",
            "run",
            "--url",
            f"http://127.0.0.1:{local_port}",
            self.tunnel,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(
                self._wait_for_registration(), timeout=START_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            await self.stop()
            raise TunnelError(
                f"Timed out waiting for cloudflared to register tunnel "
                f"{self.tunnel!r}"
            ) from None
        except TunnelError:
            await self.stop()
            raise
        self.public_url = f"https://{_bare_host(self.hostname)}"
        self._drain_task = asyncio.create_task(self._drain(), name="cloudflared-drain")
        return self.public_url

    async def _wait_for_registration(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        while True:
            line = await self._process.stdout.readline()
            if not line:
                code = await self._process.wait()
                raise TunnelError(
                    f"cloudflared exited with status {code}. Check that the "
                    f"tunnel {self.tunnel!r} exists (`cloudflared tunnel list`)"
                )
            text = line.decode("utf-8", "replace").strip()
            if text:
                logger.debug("cloudflared: %s", text)
            if READY_PATTERN.search(text):
                return
