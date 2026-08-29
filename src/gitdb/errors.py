"""Exception hierarchy for GitDb."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

__all__ = [
    "GitDbError",
    "NotFoundError",
    "ConflictError",
    "RateLimitError",
    "AuthError",
    "ValidationError",
]


class GitDbError(Exception):
    """Base class for every error raised by GitDb."""

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class NotFoundError(GitDbError):
    """A document, collection or repository path does not exist."""


class ConflictError(GitDbError):
    """A write failed because the stored blob sha changed concurrently."""


class RateLimitError(GitDbError):
    """The GitHub API rate limit was exhausted and retries were exhausted too.

    ``remaining`` and ``reset`` mirror the ``X-RateLimit-*`` headers, so callers
    can decide how long to pause before trying again.
    """

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        retry_after: Optional[float] = None,
        remaining: Optional[int] = None,
        reset: Optional[float] = None,
    ) -> None:
        super().__init__(message, status=status)
        self.retry_after = retry_after
        self.remaining = remaining
        self.reset = reset

    @property
    def reset_at(self) -> Optional[datetime]:
        """The moment the rate limit window resets, as an aware UTC datetime."""
        if self.reset is None:
            return None
        return datetime.fromtimestamp(self.reset, tz=timezone.utc)


class AuthError(GitDbError):
    """Authentication or authorization failed (or a write in read-only mode)."""


class ValidationError(GitDbError):
    """User supplied input (id, document, collection name) is invalid."""
