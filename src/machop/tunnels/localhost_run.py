"""SSH reverse tunnel to localhost.run."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
from .base import Tunnel, TunnelError

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https://([a-z0-9][a-z0-9.-]*\.lhr\.life)\b", re.IGNORECASE)
IGNORED_HOSTS = ("admin.localhost.run", "localhost.run/docs")
START_TIMEOUT_SECONDS = 30.0


class LocalhostRunTunnel(Tunnel):
    """A single `ssh -R` reverse tunnel exposing one local port."""

    name = "localhost.run"

    def __init__(
        self,
        host: str = "nokey@localhost.run",
        remote_port: int = 80,
    ) -> None:
        super().__init__()
        self.host = host
        self.remote_port = remote_port
        self._process: asyncio.subprocess.Process | None = None
        self._drain_task: asyncio.Task | None = None

    async def start(self, local_port: int) -> str:
        if self._process is not None:
            raise TunnelError("Tunnel already running")
        if shutil.which("ssh") is None:
            raise TunnelError("`ssh` not found on PATH - cannot open a tunnel")

        command = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ServerAliveInterval=20",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-R", f"{self.remote_port}:127.0.0.1:{local_port}",
            self.host,
        ]
        logger.debug("Opening tunnel: %s", " ".join(command))
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            self.public_url = await asyncio.wait_for(
                self._read_until_url(), timeout=START_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            await self.stop()
            raise TunnelError(
                "Timed out waiting for the tunnel URL. Check your network connection."
            ) from None
        except TunnelError:
            await self.stop()
            raise

        self._drain_task = asyncio.create_task(self._drain(), name="tunnel-drain")
        return self.public_url

    async def _read_until_url(self) -> str:
        assert self._process is not None and self._process.stdout is not None
        while True:
            line = await self._process.stdout.readline()
            if not line:
                code = await self._process.wait()
                raise TunnelError(
                    f"ssh exited with status {code} before providing a URL"
                )
            text = line.decode("utf-8", "replace").strip()
            if text:
                logger.debug("ssh: %s", text)
            match = URL_PATTERN.search(text)
            if match and not any(h in text for h in IGNORED_HOSTS):
                return f"https://{match.group(1)}"

    async def _drain(self) -> None:
        """Keep the pipe empty and notice if ssh dies."""
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                logger.debug("ssh: %s", line.decode("utf-8", "replace").strip())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Tunnel drain ended", exc_info=True)
        finally:
            if self._process is not None and self._process.returncode is not None:
                logger.warning("Tunnel closed (ssh exited %s)", self._process.returncode)
            self.lost.set()

    async def stop(self) -> None:
        """Terminate ssh and reap it. Never leaves an orphaned tunnel."""
        if self._drain_task is not None:
            self._drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._drain_task
            self._drain_task = None

        process, self._process = self._process, None
        self.public_url = None
        if process is None or process.returncode is not None:
            return

        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("ssh ignored SIGTERM; sending SIGKILL")
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()

