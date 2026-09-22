"""Push the public URL to ntfy when it changes."""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".config" / "machop" / "config.json"
DEFAULT_BASE_URL = "https://ntfy.sh"
_TOPIC_BYTES = 16


def generate_topic() -> str:
    return "mm-" + secrets.token_urlsafe(_TOPIC_BYTES)


def load_or_create_topic(path: Path | None = None) -> str:
    """Stable across restarts so the user subscribes exactly once."""
    path = CONFIG_PATH if path is None else path
    config: dict = {}
    try:
        loaded = json.loads(path.read_text())
        if isinstance(loaded, dict):
            config = loaded
            topic = config.get("ntfy_topic")
            if isinstance(topic, str) and topic:
                return topic
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        config = {}

    topic = generate_topic()
    config["ntfy_topic"] = topic
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2))
    os.chmod(path, 0o600)
    return topic


async def push(
    topic: str,
    message: str,
    title: str = "Machop",
    base_url: str = DEFAULT_BASE_URL,
) -> bool:
    """Best effort."""
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(
                f"{base_url}/{topic}",
                data=message.encode(),
                headers={"Title": title},
            ) as response:
                if response.status >= 400:
                    logger.warning("ntfy returned %s", response.status)
                    return False
                return True
    except Exception as exc:
        logger.warning("Could not send notification: %s", exc)
        return False
