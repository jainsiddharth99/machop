"""Session authentication."""

from __future__ import annotations

import hmac
import logging
import secrets
import string
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PIN_LENGTH = 6
PIN_TTL_SECONDS = 300.0
MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 300.0


def generate_pin(length: int = PIN_LENGTH) -> str:
    """A uniformly random numeric PIN. `secrets`, never `random`."""
    return "".join(secrets.choice(string.digits) for _ in range(length))


class AuthError(Exception):
    """Authentication failed. `retry_after` is set when throttled."""

    def __init__(self, message: str, *, status: int = 401, retry_after: float = 0.0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass
class _Attempts:
    count: int = 0
    blocked_until: float = 0.0


@dataclass
class SessionAuth:
    """Single-use PIN guarding a single viewer session."""

    pin: str
    ttl: float | None = None
    max_attempts: int = MAX_ATTEMPTS
    created_at: float = field(default_factory=time.monotonic)
    _attempts: _Attempts = field(default_factory=_Attempts)
    _claimed_by: str | None = None
    _last_claimed_by: str | None = None

    @property
    def claimed(self) -> bool:
        return self._claimed_by is not None

    def expired(self, now: float | None = None) -> bool:
        """Unused codes expire only when a TTL was explicitly requested."""
        if self.ttl is None or self.claimed:
            return False
        now = time.monotonic() if now is None else now
        return (now - self.created_at) > self.ttl

    def seconds_remaining(self, now: float | None = None) -> float | None:
        if self.ttl is None:
            return None
        now = time.monotonic() if now is None else now
        return max(0.0, self.ttl - (now - self.created_at))

    def authenticate(
        self, candidate: str, session_id: str, now: float | None = None
    ) -> bool:
        """Validate `candidate` and claim the session."""
        now = time.monotonic() if now is None else now

        if self._attempts.blocked_until > now:
            raise AuthError(
                "Too many attempts",
                status=429,
                retry_after=self._attempts.blocked_until - now,
            )

        if self.expired(now):
            raise AuthError(
                "This session code has expired. Restart machop.", status=410
            )

        if not hmac.compare_digest(self.pin, candidate):
            self._register_failure(now)
            raise AuthError("Incorrect code", status=401)

        displaced = self.claimed and not hmac.compare_digest(
            self._claimed_by or "", session_id
        )
        self._attempts = _Attempts()
        self._claimed_by = session_id
        self._last_claimed_by = session_id
        return displaced

    def _register_failure(self, now: float) -> None:
        self._attempts.count += 1
        if self._attempts.count >= self.max_attempts:
            over = self._attempts.count - self.max_attempts
            delay = min(BACKOFF_BASE_SECONDS * (2**over), BACKOFF_CAP_SECONDS)
            self._attempts.blocked_until = now + delay
            logger.warning(
                "Authentication throttled for %.0fs after %d failed attempts",
                delay,
                self._attempts.count,
            )

    def resume(self, session_id: str) -> None:
        """Re-claim a session without the PIN."""
        if not session_id or not self._last_claimed_by:
            raise AuthError("No session to resume", status=401)
        if not hmac.compare_digest(self._last_claimed_by, session_id):
            raise AuthError("No session to resume", status=401)
        self._claimed_by = session_id

    def release(self) -> None:
        """Release the claim so the same device (or another) can reconnect."""
        self._claimed_by = None
