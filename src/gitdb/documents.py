"""Transport independent document helpers shared by the sync and async clients."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union

from .errors import ValidationError

__all__ = [
    "Document",
    "utcnow",
    "dump_document",
    "encode_document",
    "decode_document",
    "loads_document",
    "with_metadata",
    "BLOB_MODE",
]

Document = Dict[str, Any]

#: Git file mode used for every document blob.
BLOB_MODE = "100644"


def utcnow() -> str:
    """Return the current time as an RFC 3339 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def dump_document(document: Mapping[str, Any]) -> bytes:
    """Serialise ``document`` to the exact bytes stored in the repository."""
    payload = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return payload.encode("utf-8")


def encode_document(document: Mapping[str, Any]) -> str:
    """Serialise ``document`` and base64-encode it for the GitHub API."""
    return base64.b64encode(dump_document(document)).decode("ascii")


def loads_document(raw: Union[str, bytes], path: Optional[str] = None) -> Document:
    """Parse stored JSON, rejecting anything that is not an object."""
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    try:
        data = json.loads(text)
    except ValueError as exc:
        where = f" at {path}" if path else ""
        raise ValidationError(f"stored document{where} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        where = f" at {path}" if path else ""
        raise ValidationError(f"stored document{where} is not a JSON object")
    return data


def decode_document(content: str, path: Optional[str] = None) -> Document:
    """Decode a base64 ``content`` field returned by the GitHub API."""
    return loads_document(base64.b64decode(content), path)


def with_metadata(
    doc_id: str,
    document: Mapping[str, Any],
    existing: Optional[Mapping[str, Any]] = None,
) -> Document:
    """Stamp ``_id``, ``_created_at``, ``_updated_at`` and ``_rev`` onto a document."""
    if not isinstance(document, Mapping):
        raise ValidationError("document must be a mapping")
    now = utcnow()
    merged: Document = dict(document)
    merged["_id"] = doc_id
    merged["_created_at"] = (existing or {}).get("_created_at") or merged.get("_created_at") or now
    merged["_updated_at"] = now
    previous_rev = (existing or {}).get("_rev")
    merged["_rev"] = int(previous_rev) + 1 if isinstance(previous_rev, int) else 1
    return merged
