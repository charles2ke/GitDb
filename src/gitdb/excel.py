"""Write documents to an Excel workbook (``.xlsx``) without extra dependencies.

An ``.xlsx`` file is a zip archive of XML parts, so the standard library is
enough: this module writes the four parts Excel requires plus one worksheet per
collection. Strings are stored inline, which keeps the writer streaming-friendly
and avoids a shared string table.

Documents are nested JSON while a worksheet is a flat grid, so values that are
not scalars are serialised back to compact JSON in their cell.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .errors import ValidationError

#: A filesystem path or any binary file object accepted by :mod:`zipfile`.
Destination = Any

__all__ = [
    "Destination",
    "columns_for",
    "document_rows",
    "cell_value",
    "write_workbook",
    "write_xlsx",
]

#: Excel refuses to open a workbook whose sheet name uses one of these.
_ILLEGAL_SHEET_CHARS = set(r"[]:*?/\\")
_SHEET_NAME_LIMIT = 31
#: Excel's hard grid limits (1048576 rows, 16384 columns); the header uses one row.
_MAX_ROWS = 1_048_576
_MAX_COLUMNS = 16_384


def columns_for(documents: Sequence[Mapping[str, Any]]) -> List[str]:
    """Return the column order for ``documents``.

    Metadata columns (``_id`` first) lead, then every other key in the order it
    was first seen, so a stable export does not depend on dict ordering luck.
    """
    seen: Dict[str, None] = {}
    for document in documents:
        for key in document:
            seen.setdefault(str(key), None)
    keys = list(seen)
    leading = [key for key in ("_id", "_rev", "_created_at", "_updated_at") if key in seen]
    return leading + [key for key in keys if key not in leading]


def document_rows(
    documents: Sequence[Mapping[str, Any]],
    columns: Optional[Sequence[str]] = None,
) -> Tuple[List[str], List[List[Any]]]:
    """Return ``(columns, rows)`` for ``documents``, missing keys becoming blanks."""
    header = list(columns) if columns is not None else columns_for(documents)
    rows = [[document.get(column) for column in header] for document in documents]
    return header, rows


def cell_value(value: Any) -> Tuple[str, str]:
    """Return the ``(type, text)`` pair used for one cell.

    ``type`` is ``"n"`` for numbers, ``"b"`` for booleans and ``"s"`` for text;
    anything that is not a scalar (lists, mappings) becomes compact JSON text.
    """
    if value is None:
        return "s", ""
    if isinstance(value, bool):
        return "b", "1" if value else "0"
    if isinstance(value, int):
        return "n", str(value)
    if isinstance(value, float):
        # Excel has no encoding for NaN/inf, so keep them readable as text.
        if value != value or value in (float("inf"), float("-inf")):
            return "s", repr(value)
        return "n", repr(value)
    if isinstance(value, (datetime, date)):
        return "s", value.isoformat()
    if isinstance(value, str):
        return "s", value
    return "s", json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _escape(text: str) -> str:
    cleaned = "".join(
        character
        for character in text
        # XML 1.0 forbids most control characters outside tab/newline/return.
        if character in "\t\n\r" or character >= " "
    )
    return (
        cleaned.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _column_name(index: int) -> str:
    """Return the Excel column name for a zero-based ``index`` (0 -> ``A``)."""
    name = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def _sheet_name(name: str, used: Sequence[str]) -> str:
    cleaned = "".join(" " if character in _ILLEGAL_SHEET_CHARS else character for character in name)
    cleaned = cleaned.strip("'").strip() or "Sheet"
    cleaned = cleaned[:_SHEET_NAME_LIMIT]
    candidate, suffix = cleaned, 2
    while candidate.lower() in {existing.lower() for existing in used}:
        tail = f"~{suffix}"
        candidate = cleaned[: _SHEET_NAME_LIMIT - len(tail)] + tail
        suffix += 1
    return candidate


def _cell_xml(reference: str, value: Any) -> str:
    kind, text = cell_value(value)
    if kind == "s":
        if not text:
            return f'<c r="{reference}" t="inlineStr"/>'
        return f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{_escape(text)}</t></is></c>'  # noqa: E501
    if kind == "b":
        return f'<c r="{reference}" t="b"><v>{text}</v></c>'
    return f'<c r="{reference}"><v>{text}</v></c>'


def _sheet_xml(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    if len(columns) > _MAX_COLUMNS:
        raise ValidationError(f"a worksheet holds at most {_MAX_COLUMNS} columns")
    if len(rows) + 1 > _MAX_ROWS:
        raise ValidationError(f"a worksheet holds at most {_MAX_ROWS - 1} data rows")
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]
    for number, values in enumerate([list(columns)] + [list(row) for row in rows], 1):
        cells = "".join(
            _cell_xml(f"{_column_name(index)}{number}", value)
            for index, value in enumerate(values[: len(columns)])
        )
        parts.append(f'<row r="{number}">{cells}</row>')
    parts.append("</sheetData></worksheet>")
    return "".join(parts)


def _content_types(count: int) -> str:
    sheets = "".join(
        f'<Override PartName="/xl/worksheets/sheet{number}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.worksheet+xml"/>'
        for number in range(1, count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.sheet.main+xml"/>'
        f"{sheets}</Types>"
    )


def _workbook_xml(names: Sequence[str]) -> str:
    sheets = "".join(
        f'<sheet name="{_escape(name)}" sheetId="{number}" r:id="rId{number}"/>'
        for number, name in enumerate(names, 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets}</sheets></workbook>"
    )


def _workbook_rels(count: int) -> str:
    relationships = "".join(
        f'<Relationship Id="rId{number}" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{number}.xml"/>'
        for number in range(1, count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{relationships}</Relationships>"
    )


_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
)


def write_workbook(
    destination: Destination,
    sheets: Iterable[Tuple[str, Sequence[str], Sequence[Sequence[Any]]]],
) -> None:
    """Write ``sheets`` (``name, columns, rows`` triples) as an ``.xlsx`` workbook.

    ``destination`` is a path or any binary file object. Sheet names are
    sanitised and de-duplicated the way Excel requires.
    """
    prepared = [
        (name, list(columns), [list(row) for row in rows]) for name, columns, rows in sheets
    ]
    if not prepared:
        raise ValidationError("a workbook needs at least one sheet")
    names: List[str] = []
    for name, _, _ in prepared:
        names.append(_sheet_name(name, names))
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types(len(prepared)))
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", _workbook_xml(names))
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(prepared)))
        for number, (_, columns, rows) in enumerate(prepared, 1):
            archive.writestr(f"xl/worksheets/sheet{number}.xml", _sheet_xml(columns, rows))


def write_xlsx(
    destination: Destination,
    documents: Sequence[Mapping[str, Any]],
    *,
    columns: Optional[Sequence[str]] = None,
    sheet_name: str = "Sheet1",
) -> List[str]:
    """Write ``documents`` to a single-sheet workbook and return the columns used."""
    header, rows = document_rows(documents, columns)
    write_workbook(destination, [(sheet_name, header, rows)])
    return header
