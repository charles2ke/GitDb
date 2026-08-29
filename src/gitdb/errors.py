"""Exception hierarchy for GitDb."""

from __future__ import annotations

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
    """The GitHub API rate limit was exhausted and retries were exhausted too."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message, status=status)
        self.retry_after = retry_after


class AuthError(GitDbError):
    """Authentication or authorization failed (or a write in read-only mode)."""


class ValidationError(GitDbError):
    """User supplied input (id, document, collection name) is invalid."""
