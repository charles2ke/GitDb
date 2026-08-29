"""GitDb - use a GitHub repository as a lightweight document database."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:  # pragma: no cover - import cycle free type hints
    from .aio import AsyncBatch, AsyncCollection, AsyncGitDb

__version__ = "0.2.0"

__all__ = [
    "GitDb",
    "Collection",
    "AsyncGitDb",
    "AsyncCollection",
    "AsyncBatch",
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

#: The asyncio client lives behind the optional ``async`` extra, so it is only
#: imported when actually asked for. ``pip install "gitdb-py[async]"``.
_ASYNC_EXPORTS = {"AsyncGitDb", "AsyncCollection", "AsyncBatch"}


def __getattr__(name: str) -> Any:
    if name in _ASYNC_EXPORTS:
        from . import aio

        return getattr(aio, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
