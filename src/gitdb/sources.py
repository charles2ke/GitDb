"""Read records out of other databases so they can be stored in GitDb.

GitDb documents are JSON objects, so every source ends up as an iterable of
mappings with JSON-safe values. Two shapes are supported:

* **Relational** - anything that speaks :pep:`249` (sqlite3, psycopg, MySQL
  connectors, ...): :func:`from_sql` runs a query and :func:`from_dbapi` walks a
  cursor you opened yourself, turning ``cursor.description`` into column names.
* **Non-relational** - MongoDB-style collections and any other iterable of
  mappings: :func:`from_mongo` and :func:`from_records`.

No database driver is imported here; the caller passes an already connected
object, which keeps GitDb dependency free and works with any driver.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Mapping, Sequence, Set
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .errors import ValidationError

__all__ = [
    "normalize_value",
    "normalize_record",
    "from_records",
    "from_dbapi",
    "from_sql",
    "from_mongo",
    "record_id",
]

#: Batch size used when a cursor is drained with ``fetchmany``.
FETCH_SIZE = 500


def normalize_value(value: Any) -> Any:
    """Return ``value`` as something :mod:`json` can serialise.

    Database drivers hand back types JSON does not know: dates, decimals,
    binary columns and driver-specific ids such as MongoDB's ``ObjectId``.
    Scalars are kept as-is, containers are converted recursively and anything
    still unknown falls back to ``str(value)``.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # NaN/inf are not valid JSON, so keep them as text instead of silently
        # writing a document no strict JSON parser can read back.
        return (
            value if value == value and value not in (float("inf"), float("-inf")) else str(value)
        )
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        # Binary columns are not valid UTF-8 in general, so decoding them
        # would silently corrupt the data. Base64 is a lossless, JSON-safe
        # representation instead.
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, Mapping):
        return {str(key): normalize_value(item) for key, item in value.items()}
    if isinstance(value, (Sequence, Set)) and not isinstance(value, (str, bytes)):
        return [normalize_value(item) for item in value]
    return str(value)


def normalize_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Return ``record`` as a JSON-safe document."""
    if not isinstance(record, Mapping):
        raise ValidationError(f"record must be a mapping, got {type(record).__name__}")
    return {str(key): normalize_value(value) for key, value in record.items()}


def from_records(records: Iterable[Mapping[str, Any]]) -> Iterator[Dict[str, Any]]:
    """Normalise an iterable of mappings (any non-relational source)."""
    for record in records:
        yield normalize_record(record)


def _column_names(cursor: Any) -> List[str]:
    description = getattr(cursor, "description", None)
    if not description:
        raise ValidationError("cursor has no description; run a SELECT before importing")
    names: List[str] = []
    seen: set[str] = set()
    for position, column in enumerate(description):
        name = str(column[0]) if column[0] is not None else f"column_{position}"
        # Duplicate labels (``SELECT a.id, b.id``) would otherwise shadow each other.
        # Keep incrementing the suffix until it doesn't collide with an
        # existing (real or previously disambiguated) name.
        if name in seen:
            suffix = position
            candidate = f"{name}_{suffix}"
            while candidate in seen:
                suffix += 1
                candidate = f"{name}_{suffix}"
            name = candidate
        names.append(name)
        seen.add(name)
    return names


def from_dbapi(cursor: Any, *, fetch_size: int = FETCH_SIZE) -> Iterator[Dict[str, Any]]:
    """Yield one document per row of an executed :pep:`249` ``cursor``.

    Rows are pulled in ``fetch_size`` chunks so large tables never have to fit
    in memory. Rows that are already mappings (``sqlite3.Row``, dict cursors)
    keep their own keys; tuples are zipped with ``cursor.description``.
    """
    if fetch_size < 1:
        raise ValidationError("fetch_size must be positive")
    columns = _column_names(cursor)
    while True:
        rows = cursor.fetchmany(fetch_size)
        if not rows:
            return
        for row in rows:
            if isinstance(row, Mapping):
                yield normalize_record(row)
                continue
            values = list(row)
            if len(values) != len(columns):
                raise ValidationError(
                    f"row has {len(values)} values but the cursor describes {len(columns)} columns"
                )
            yield normalize_record(dict(zip(columns, values)))


def from_sql(
    connection: Any,
    query: str,
    parameters: Any = None,
    *,
    fetch_size: int = FETCH_SIZE,
) -> Iterator[Dict[str, Any]]:
    """Run ``query`` on a :pep:`249` ``connection`` and yield its rows as documents.

    The cursor is closed once the iterator is exhausted or garbage collected.
    """
    cursor = connection.cursor()
    try:
        if parameters is None:
            cursor.execute(query)
        else:
            cursor.execute(query, parameters)
        yield from from_dbapi(cursor, fetch_size=fetch_size)
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()


def from_mongo(
    collection: Any,
    filter: Optional[Mapping[str, Any]] = None,
    *,
    projection: Optional[Mapping[str, Any]] = None,
    limit: Optional[int] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield documents from a MongoDB-style ``collection`` (anything with ``find``).

    ``ObjectId`` values are stringified by :func:`normalize_value`, so the
    resulting documents are plain JSON.
    """
    find = getattr(collection, "find", None)
    if not callable(find):
        raise ValidationError("collection must expose a find() method")
    cursor = find(dict(filter or {}), dict(projection) if projection else None)
    if limit is not None:
        if limit < 0:
            raise ValidationError("limit must not be negative")
        cursor = cursor.limit(limit)
    yield from from_records(cursor)


def record_id(record: Mapping[str, Any], *, id_field: str = "_id") -> Optional[str]:
    """Return the document id carried by ``record``, or ``None`` when absent."""
    value = record.get(id_field)
    if value is None:
        return None
    text = str(value).strip()
    return text or None
