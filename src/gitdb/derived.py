"""Secondary indexes and collection manifests.

Both are ordinary JSON files kept next to the data (``{root}/_index/...`` and
``{root}/_manifest/...``) and rewritten in the *same commit* as the documents
they describe, so they can never disagree with the data by more than one commit.
Everything in this module is pure, which lets the sync and async clients share it.
"""

from __future__ import annotations

import json
from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Dict, List, Optional, Tuple

from .documents import Document, utcnow

__all__ = [
    "CollectionConfig",
    "index_key",
    "index_values",
    "empty_index",
    "apply_index",
    "build_index",
    "lookup_index",
    "empty_manifest",
    "apply_manifest",
    "build_manifest",
    "manifest_ids",
    "manifest_documents",
]


class CollectionConfig:
    """Which derived files (if any) a collection maintains.

    Derived files are opt-in per collection because they cost write
    amplification: every document write also rewrites the index and manifest
    blobs (in the same commit, so they stay consistent).
    """

    __slots__ = ("name", "indexes", "manifest", "manifest_fields")

    def __init__(
        self,
        name: str,
        *,
        indexes: Sequence[str] = (),
        manifest: bool = False,
        manifest_fields: Sequence[str] = (),
    ) -> None:
        self.name = name
        self.indexes: Tuple[str, ...] = tuple(dict.fromkeys(indexes))
        self.manifest = manifest or bool(manifest_fields)
        self.manifest_fields: Tuple[str, ...] = tuple(dict.fromkeys(manifest_fields))

    @property
    def derived(self) -> bool:
        """True when writes must also rewrite index/manifest files."""
        return bool(self.indexes) or self.manifest

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"CollectionConfig(name={self.name!r}, indexes={self.indexes!r}, "
            f"manifest={self.manifest!r})"
        )


def index_key(value: Any) -> str:
    """Return the index key for ``value``.

    Strings are used verbatim; every other scalar is JSON encoded. Values are
    therefore matched by their string form, so ``1`` and ``"1"`` share a key.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def index_values(document: Mapping[str, Any], field: str) -> List[str]:
    """Return every index key a document contributes for ``field``.

    Missing fields contribute nothing; list/tuple values are indexed per element
    so tag-style fields work as expected.
    """
    if field not in document:
        return []
    value = document[field]
    if isinstance(value, (list, tuple)):
        return list(dict.fromkeys(index_key(item) for item in value))
    return [index_key(value)]


def empty_index(collection: str, field: str) -> Document:
    return {
        "_index": field,
        "collection": collection,
        "updated_at": utcnow(),
        "values": {},
        "ids": {},
    }


def _index_parts(index: Mapping[str, Any]) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    values = index.get("values")
    ids = index.get("ids")
    # Buckets are kept sorted so ids can be inserted and removed with a binary search.
    values = (
        {str(key): sorted(item) for key, item in values.items()} if isinstance(values, dict) else {}
    )
    ids = {str(key): list(item) for key, item in ids.items()} if isinstance(ids, dict) else {}
    return values, ids


def apply_index(
    index: Mapping[str, Any],
    collection: str,
    field: str,
    changes: Mapping[str, Optional[Mapping[str, Any]]],
) -> Document:
    """Return ``index`` with ``changes`` applied (``None`` document means delete)."""
    values, ids = _index_parts(index)
    for doc_id, document in changes.items():
        for stale in ids.pop(doc_id, []):
            bucket = values.get(stale)
            if bucket is None:
                continue
            position = bisect_left(bucket, doc_id)
            if position < len(bucket) and bucket[position] == doc_id:
                del bucket[position]
            if not bucket:
                values.pop(stale, None)
        if document is None:
            continue
        keys = index_values(document, field)
        if not keys:
            continue
        ids[doc_id] = keys
        for key in keys:
            bucket = values.setdefault(key, [])
            # Buckets stay sorted, so a binary search finds the insertion point.
            position = bisect_left(bucket, doc_id)
            if position == len(bucket) or bucket[position] != doc_id:
                bucket.insert(position, doc_id)
    return {
        "_index": field,
        "collection": collection,
        "updated_at": utcnow(),
        "values": {key: values[key] for key in sorted(values)},
        "ids": {key: ids[key] for key in sorted(ids)},
    }


def build_index(
    collection: str,
    field: str,
    documents: Mapping[str, Mapping[str, Any]],
) -> Document:
    """Rebuild an index from scratch out of every document in a collection."""
    return apply_index(empty_index(collection, field), collection, field, dict(documents))


def lookup_index(index: Mapping[str, Any], value: Any) -> List[str]:
    """Return the document ids stored for ``value``."""
    values = index.get("values")
    if not isinstance(values, Mapping):
        return []
    bucket = values.get(index_key(value))
    if not isinstance(bucket, Iterable) or isinstance(bucket, (str, bytes)):
        return []
    return [str(doc_id) for doc_id in bucket]


def empty_manifest(collection: str) -> Document:
    return {
        "collection": collection,
        "updated_at": utcnow(),
        "count": 0,
        "ids": [],
        "documents": {},
    }


def _projection(document: Mapping[str, Any], fields: Sequence[str]) -> Dict[str, Any]:
    return {field: document[field] for field in fields if field in document}


def apply_manifest(
    manifest: Mapping[str, Any],
    collection: str,
    changes: Mapping[str, Optional[Mapping[str, Any]]],
    fields: Sequence[str] = (),
) -> Document:
    """Return ``manifest`` with ``changes`` applied (``None`` document means delete)."""
    ids = set(manifest_ids(manifest))
    projected = dict(manifest_documents(manifest))
    for doc_id, document in changes.items():
        if document is None:
            ids.discard(doc_id)
            projected.pop(doc_id, None)
            continue
        ids.add(doc_id)
        if fields:
            projected[doc_id] = _projection(document, fields)
    ordered = sorted(ids)
    return {
        "collection": collection,
        "updated_at": utcnow(),
        "count": len(ordered),
        "ids": ordered,
        "documents": {doc_id: projected[doc_id] for doc_id in ordered if doc_id in projected},
    }


def build_manifest(
    collection: str,
    documents: Mapping[str, Mapping[str, Any]],
    fields: Sequence[str] = (),
) -> Document:
    """Rebuild a manifest from scratch out of every document in a collection."""
    return apply_manifest(empty_manifest(collection), collection, dict(documents), fields)


def manifest_ids(manifest: Mapping[str, Any]) -> List[str]:
    ids = manifest.get("ids")
    if not isinstance(ids, Iterable) or isinstance(ids, (str, bytes)):
        return []
    return [str(doc_id) for doc_id in ids]


def manifest_documents(manifest: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    documents = manifest.get("documents")
    if not isinstance(documents, Mapping):
        return {}
    return {str(key): dict(value) for key, value in documents.items() if isinstance(value, Mapping)}
