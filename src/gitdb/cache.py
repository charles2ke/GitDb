"""Pluggable cache for blob shas, ETags and decoded documents.

The default :class:`MemoryCache` keeps everything in the process. Supply any
object implementing the :class:`Cache` interface (for example a disk or Redis
backed one) through ``GitDb(cache=...)`` to share validators across processes,
which turns most reads into cheap conditional requests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Mapping, Optional

__all__ = ["CacheEntry", "Cache", "MemoryCache", "NullCache"]


class CacheEntry:
    """Everything GitDb knows about one stored path.

    ``sha`` is the Git blob sha (used for optimistic concurrency), ``etag`` the
    HTTP validator for conditional requests and ``document`` the decoded body
    that a ``304 Not Modified`` response allows us to reuse.
    """

    __slots__ = ("sha", "etag", "document")

    def __init__(
        self,
        sha: Optional[str] = None,
        etag: Optional[str] = None,
        document: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.sha = sha
        self.etag = etag
        self.document = document

    def merge(self, other: CacheEntry) -> CacheEntry:
        """Return a copy of ``self`` updated with the populated fields of ``other``.

        A different blob sha means the stored body and ETag describe an older
        revision of the path, so they are dropped instead of carried over.
        """
        if other.sha is not None and self.sha is not None and other.sha != self.sha:
            return CacheEntry(sha=other.sha, etag=other.etag, document=other.document)
        return CacheEntry(
            sha=other.sha if other.sha is not None else self.sha,
            etag=other.etag if other.etag is not None else self.etag,
            document=other.document if other.document is not None else self.document,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CacheEntry):
            return NotImplemented
        return (self.sha, self.etag, self.document) == (other.sha, other.etag, other.document)

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"CacheEntry(sha={self.sha!r}, etag={self.etag!r})"


class Cache(ABC):
    """Minimal cache interface used by :class:`~gitdb.client.GitDb`."""

    @abstractmethod
    def get(self, path: str) -> Optional[CacheEntry]:
        """Return the entry stored for ``path`` (or ``None``)."""

    @abstractmethod
    def set(self, path: str, entry: CacheEntry) -> None:
        """Store ``entry`` for ``path``, merging with anything already known."""

    @abstractmethod
    def delete(self, path: str) -> None:
        """Forget everything stored for ``path``."""

    @abstractmethod
    def clear(self) -> None:
        """Forget every entry."""

    def snapshot(self) -> Mapping[str, CacheEntry]:
        """Return the currently cached entries (best effort, for introspection)."""
        return {}


class MemoryCache(Cache):
    """An unbounded in-process cache (the default)."""

    def __init__(self) -> None:
        self._entries: Dict[str, CacheEntry] = {}

    def get(self, path: str) -> Optional[CacheEntry]:
        return self._entries.get(path)

    def set(self, path: str, entry: CacheEntry) -> None:
        existing = self._entries.get(path)
        self._entries[path] = existing.merge(entry) if existing else entry

    def delete(self, path: str) -> None:
        self._entries.pop(path, None)

    def clear(self) -> None:
        self._entries.clear()

    def snapshot(self) -> Mapping[str, CacheEntry]:
        return dict(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


class NullCache(Cache):
    """A cache that stores nothing (``GitDb(cache=False)``)."""

    def get(self, path: str) -> Optional[CacheEntry]:
        return None

    def set(self, path: str, entry: CacheEntry) -> None:
        return None

    def delete(self, path: str) -> None:
        return None

    def clear(self) -> None:
        return None
