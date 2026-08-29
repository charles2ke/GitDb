"""Repository path layout shared by the sync and async clients."""

from __future__ import annotations

from typing import List, Optional

from .errors import ValidationError
from .ids import validate_id, validate_name

__all__ = ["PathResolver", "INDEX_DIR", "MANIFEST_DIR"]

#: Directory holding the secondary index files.
INDEX_DIR = "_index"

#: Directory holding the per-collection manifests.
MANIFEST_DIR = "_manifest"


class PathResolver:
    """Translate collections, ids and index fields into repository paths."""

    __slots__ = ("root", "shard_depth", "shard_width")

    def __init__(self, root: str = "data", *, shard_depth: int = 0, shard_width: int = 2) -> None:
        if shard_depth < 0 or shard_width < 1:
            raise ValidationError("shard_depth must be >= 0 and shard_width >= 1")
        self.root = root.strip("/")
        self.shard_depth = shard_depth
        self.shard_width = shard_width

    def _under_root(self, *parts: str) -> str:
        segments = [self.root, *parts] if self.root else list(parts)
        return "/".join(segment for segment in segments if segment)

    def collection_path(self, collection: str) -> str:
        validate_name(collection)
        return self._under_root(collection)

    def shard_for(self, doc_id: str) -> List[str]:
        """Return the shard directory names for ``doc_id`` (empty without sharding)."""
        shards: List[str] = []
        for level in range(self.shard_depth):
            start = level * self.shard_width
            chunk = doc_id[start : start + self.shard_width]
            if not chunk:
                break
            shards.append(chunk)
        return shards

    def document_path(self, collection: str, doc_id: str) -> str:
        validate_id(doc_id)
        parts = [self.collection_path(collection), *self.shard_for(doc_id)]
        parts.append(f"{doc_id}.json")
        return "/".join(parts)

    def index_path(self, collection: str, field: str) -> str:
        validate_name(collection)
        validate_name(field, kind="index field")
        return self._under_root(INDEX_DIR, collection, f"{field}.json")

    def manifest_path(self, collection: str) -> str:
        validate_name(collection)
        return self._under_root(MANIFEST_DIR, f"{collection}.json")

    @staticmethod
    def id_from_path(path: str) -> Optional[str]:
        name = path.rsplit("/", 1)[-1]
        return name[:-5] if name.endswith(".json") else None
