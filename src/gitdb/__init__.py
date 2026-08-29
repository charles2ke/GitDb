"""GitDb - use a GitHub repository as a lightweight document database."""

from __future__ import annotations

from .auth import InstallationTokenAuth
from .batch import Batch, Transaction, Writer
from .cache import Cache, CacheEntry, MemoryCache, NullCache
from .client import Collection, GitDb, Page, TreeEntry
from .derived import CollectionConfig
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
from .ratelimit import RateLimit

__version__ = "0.2.0"

__all__ = [
    "GitDb",
    "Collection",
    "Batch",
    "Writer",
    "Transaction",
    "Page",
    "TreeEntry",
    "CollectionConfig",
    "GitHubClient",
    "Cache",
    "CacheEntry",
    "MemoryCache",
    "NullCache",
    "RateLimit",
    "InstallationTokenAuth",
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
