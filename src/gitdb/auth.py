"""Authentication helpers, including refreshing GitHub App installation tokens."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Tuple, Union

from .errors import AuthError

__all__ = ["InstallationTokenAuth"]

#: A callable returning ``(token, expires_at)``. ``expires_at`` may be an RFC 3339
#: string (what ``POST /app/installations/{id}/access_tokens`` returns), a
#: datetime, epoch seconds, or ``None`` for "never expires".
TokenFactory = Callable[[], Union[str, Tuple[str, Any]]]


def _as_epoch(expires_at: Any) -> Optional[float]:
    if expires_at is None:
        return None
    if isinstance(expires_at, (int, float)):
        return float(expires_at)
    if isinstance(expires_at, datetime):
        moment = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    if isinstance(expires_at, str):
        text = expires_at.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    return None


class InstallationTokenAuth:
    """A ``requests``-compatible auth object for short-lived GitHub tokens.

    GitHub App installation tokens lift the 5,000 requests/hour personal token
    quota but expire after an hour. Pass a callable that mints a fresh token and
    GitDb refreshes it automatically shortly before it expires::

        auth = InstallationTokenAuth(mint_installation_token)
        db = GitDb("owner/name", auth=auth)

    The callable must return either a token string or a ``(token, expires_at)``
    pair. Minting is left to the caller so GitDb needs no JWT/crypto dependency.
    """

    def __init__(self, factory: TokenFactory, *, leeway: float = 60.0) -> None:
        if not callable(factory):
            raise AuthError("InstallationTokenAuth requires a callable token factory")
        self._factory = factory
        self._leeway = max(0.0, leeway)
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at: Optional[float] = None

    def _expired(self) -> bool:
        if self._token is None:
            return True
        if self._expires_at is None:
            return False
        return time.time() >= self._expires_at - self._leeway

    def token(self) -> str:
        """Return a valid token, minting a new one when the current one is stale."""
        with self._lock:
            if self._expired():
                result = self._factory()
                token, expires_at = result if isinstance(result, tuple) else (result, None)
                if not isinstance(token, str) or not token:
                    raise AuthError("token factory did not return a token")
                self._token = token
                self._expires_at = _as_epoch(expires_at)
            current = self._token
        if current is None:  # pragma: no cover - defensive
            raise AuthError("token factory did not return a token")
        return current

    def __call__(self, request: Any) -> Any:
        request.headers["Authorization"] = "Bearer " + self.token()
        return request
