"""ngrok tunnel - the one backend here that can give a permanent address."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil

from .base import Tunnel, TunnelError

logger = logging.getLogger(__name__)

START_TIMEOUT_SECONDS = 45.0

ERROR_HINTS = {
    "ERR_NGROK_4018": (
        "ngrok needs an authtoken. Create a free account at "
        "https://dashboard.ngrok.com and run: ngrok config add-authtoken <token>"
    ),
    "ERR_NGROK_313": (
        "That ngrok domain is not registered to your account. Claim your free "
        "static domain at https://dashboard.ngrok.com/domains"
    ),
    "ERR_NGROK_108": (
        "This ngrok account already has an agent online. Stop the other one, "
        "or use --tunnel cloudflared for this session."
    ),
}


class NgrokTunnel(Tunnel):
    name = "ngrok"

    def __init__(self, domain: str | None = None) -> None:
        super().__init__()
        self.domain = domain
        self._process: asyncio.subprocess.Process | None = None
        self._drain_task: asyncio.Task | None = None

    async def start(self, local_port: int) -> str:
        if shutil.which("ngrok") is None:
            raise TunnelError(
                "`ngrok` is not installed. Install it with `brew install ngrok`, "
                "or use --tunnel cloudflared for a random URL that needs no account."
            )
        command = [
            "ngrok",
            "http",
            str(local_port),
            "--log",
            "stdout",
            "--log-format",
            "json",
        ]
        if self.domain:
            command += ["--url", self._normalise_domain(self.domain)]

        self._process = await asyncio.create_subprocess_exec(
            *command,
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
            raise TunnelError("Timed out waiting for ngrok to open the tunnel") from None
        except TunnelError:
            await self.stop()
            raise
        self._drain_task = asyncio.create_task(self._drain(), name="ngrok-drain")
        return self.public_url

    @staticmethod
    def _normalise_domain(domain: str) -> str:
        """Accept `foo.ngrok-free.app` or the full https:// form alike."""
        return domain if "://" in domain else f"https://{domain}"

    async def _read_until_ready(self) -> str:
        assert self._process is not None and self._process.stdout is not None
        while True:
            line = await self._process.stdout.readline()
            if not line:
                code = await self._process.wait()
                raise TunnelError(f"ngrok exited with status {code}")
            record = _parse(line)
            if record is None:
                continue
            logger.debug("ngrok: %s", record)
            url = record.get("url")
            if record.get("msg") == "started tunnel" and isinstance(url, str):
                return url.replace("http://", "https://", 1)
            failure = _failure(record)
            if failure:
                raise TunnelError(failure)

    async def _drain(self) -> None:
        """Keep the pipe empty; a full 64 KiB buffer would block the child."""
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                logger.debug("ngrok: %s", line.decode("utf-8", "replace").strip())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("ngrok drain ended", exc_info=True)
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


def _parse(line: bytes) -> dict | None:
    text = line.decode("utf-8", "replace").strip()
    if not text.startswith("{"):
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _failure(record: dict) -> str | None:
    """Turn an ngrok error record into something a user can act on."""
    if str(record.get("lvl", "")).lower() not in ("eror", "error", "crit"):
        return None
    blob = " ".join(str(v) for v in record.values())
    for code, hint in ERROR_HINTS.items():
        if code in blob:
            return hint
    return f"ngrok failed: {record.get('err') or record.get('msg') or blob}"
