"""Identifier generation and validation helpers."""

from __future__ import annotations

import os
import re
import time
import uuid

from .errors import ValidationError

__all__ = ["new_id", "validate_id", "validate_name"]

#: Crockford base32 alphabet (no I, L, O, U) used for the sortable ids.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: Ids are restricted to a filesystem/URL safe charset.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: Collection names follow the same rules but may also contain ``/``-free names only.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

MAX_ID_LENGTH = 128


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(_ALPHABET[rem])
    return "".join(reversed(chars))


def new_id() -> str:
    """Return a new lexicographically sortable, ULID-style identifier.

    The first 10 characters encode the current time in milliseconds, the
    remaining 16 characters are random. Ids sort by creation time, which keeps
    directory listings and pagination stable.
    """
    timestamp = int(time.time() * 1000)
    randomness = int.from_bytes(os.urandom(10), "big")
    return _encode(timestamp, 10) + _encode(randomness, 16)


def new_uuid() -> str:
    """Return a random uuid4 identifier (non-sortable alternative to :func:`new_id`)."""
    return str(uuid.uuid4())


def validate_id(doc_id: str) -> str:
    """Validate a user supplied document id.

    Rejects empty values, path traversal (``..``, ``/``, ``\\``), absolute
    paths and any character outside ``[A-Za-z0-9._-]``.
    """
    if not isinstance(doc_id, str):
        raise ValidationError(f"document id must be a string, got {type(doc_id).__name__}")
    if not doc_id:
        raise ValidationError("document id must not be empty")
    if len(doc_id) > MAX_ID_LENGTH:
        raise ValidationError(f"document id must be at most {MAX_ID_LENGTH} characters")
    if doc_id in {".", ".."} or ".." in doc_id:
        raise ValidationError(f"invalid document id: {doc_id!r}")
    if not _ID_RE.match(doc_id):
        raise ValidationError(
            f"invalid document id: {doc_id!r} "
            "(allowed: letters, digits, '.', '_' and '-', starting alphanumeric)"
        )
    return doc_id


def validate_name(name: str, *, kind: str = "collection") -> str:
    """Validate a collection name using the same safe charset as ids."""
    if not isinstance(name, str):
        raise ValidationError(f"{kind} name must be a string, got {type(name).__name__}")
    if not name:
        raise ValidationError(f"{kind} name must not be empty")
    if ".." in name or not _NAME_RE.match(name):
        raise ValidationError(f"invalid {kind} name: {name!r}")
    return name
