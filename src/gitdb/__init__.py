"""GitDb - use a GitHub repository as a lightweight document database."""

from __future__ import annotations

from .client import Batch, Collection, GitDb
from .errors import (
    AuthError,
    ConflictError,
    GitDbError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)
from .http import DEFAULT_API_URL, DEFAULT_RAW_URL, GitHubClient
from .ids import new_id, new_uuid, validate_id

__version__ = "0.1.0"

__all__ = [
    "GitDb",
    "Collection",
    "Batch",
    "GitHubClient",
    "GitDbError",
    "NotFoundError",
    "ConflictError",
    "RateLimitError",
    "AuthError",
    "ValidationError",
    "new_id",
    "new_uuid",
    "validate_id",
    "DEFAULT_API_URL",
    "DEFAULT_RAW_URL",
    "__version__",
]
