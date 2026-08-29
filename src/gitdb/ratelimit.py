"""Rate-limit bookkeeping and proactive client-side pacing."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Optional

__all__ = ["RateLimit", "RateLimiter"]


def _as_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _as_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class RateLimit:
    """A snapshot of the GitHub rate limit for one resource."""

    __slots__ = ("limit", "remaining", "reset", "used", "resource")

    def __init__(
        self,
        *,
        limit: Optional[int] = None,
        remaining: Optional[int] = None,
        reset: Optional[float] = None,
        used: Optional[int] = None,
        resource: str = "core",
    ) -> None:
        self.limit = limit
        self.remaining = remaining
        self.reset = reset
        self.used = used
        self.resource = resource

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> Optional[RateLimit]:
        """Build a snapshot from ``X-RateLimit-*`` response headers."""
        remaining = _as_int(headers.get("X-RateLimit-Remaining"))
        limit = _as_int(headers.get("X-RateLimit-Limit"))
        if remaining is None and limit is None:
            return None
        return cls(
            limit=limit,
            remaining=remaining,
            reset=_as_float(headers.get("X-RateLimit-Reset")),
            used=_as_int(headers.get("X-RateLimit-Used")),
            resource=headers.get("X-RateLimit-Resource", "core"),
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, object], resource: str = "core") -> RateLimit:
        """Build a snapshot from a ``GET /rate_limit`` response body."""
        resources = payload.get("resources")
        source: Mapping[str, object] = {}
        if isinstance(resources, Mapping):
            candidate = resources.get(resource)
            if isinstance(candidate, Mapping):
                source = candidate
        if not source:
            rate = payload.get("rate")
            if isinstance(rate, Mapping):
                source = rate
        return cls(
            limit=_coerce_int(source.get("limit")),
            remaining=_coerce_int(source.get("remaining")),
            reset=_coerce_float(source.get("reset")),
            used=_coerce_int(source.get("used")),
            resource=resource,
        )

    @property
    def reset_at(self) -> Optional[datetime]:
        """The moment the window resets, as an aware UTC datetime."""
        if self.reset is None:
            return None
        return datetime.fromtimestamp(self.reset, tz=timezone.utc)

    @property
    def seconds_until_reset(self) -> float:
        if self.reset is None:
            return 0.0
        return max(0.0, self.reset - time.time())

    @property
    def exhausted(self) -> bool:
        return self.remaining is not None and self.remaining <= 0

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"RateLimit(resource={self.resource!r}, remaining={self.remaining}, "
            f"limit={self.limit}, reset_in={self.seconds_until_reset:.0f}s)"
        )


def _coerce_int(value: object) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) else None


def _coerce_float(value: object) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


class RateLimiter:
    """Spread requests over the remaining quota instead of only reacting to 403s.

    The limiter tracks the ``X-RateLimit-*`` headers of every response. Once the
    remaining quota drops below ``threshold`` (a fraction of the window) it
    returns the delay needed to spread the surviving requests evenly over the
    time left until the reset, capped by ``max_delay``.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        threshold: float = 0.1,
        max_delay: float = 60.0,
    ) -> None:
        self.enabled = enabled
        self.threshold = threshold
        self.max_delay = max_delay
        self._lock = threading.Lock()
        self._state: Optional[RateLimit] = None

    @property
    def state(self) -> Optional[RateLimit]:
        with self._lock:
            return self._state

    def observe(self, headers: Mapping[str, str]) -> None:
        snapshot = RateLimit.from_headers(headers)
        if snapshot is None or snapshot.resource not in ("core", "graphql"):
            return
        with self._lock:
            self._state = snapshot

    def delay(self) -> float:
        """Return how long to wait before the next request (``0`` when free)."""
        if not self.enabled:
            return 0.0
        snapshot = self.state
        if snapshot is None or snapshot.remaining is None:
            return 0.0
        wait = snapshot.seconds_until_reset
        if snapshot.remaining <= 0:
            return min(self.max_delay, wait)
        budget = snapshot.limit or 0
        if budget and snapshot.remaining > budget * self.threshold:
            return 0.0
        if not budget and snapshot.remaining > 1:
            return 0.0
        return min(self.max_delay, wait / max(1, snapshot.remaining))
