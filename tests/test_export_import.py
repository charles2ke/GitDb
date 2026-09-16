"""Excel export and relational / non-relational import."""

from __future__ import annotations

import base64
import io
import re
import sqlite3
import zipfile
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence
from uuid import UUID

import pytest
import responses

from gitdb import GitDb, ValidationError
from gitdb.excel import cell_value, columns_for, write_workbook, write_xlsx
from gitdb.sources import (
    from_dbapi,
    from_mongo,
    from_records,
    from_sql,
    normalize_record,
    normalize_value,
)
from tests.conftest import API, REPO, decode, register_commit_endpoints, register_documents


def sheet_xml(data: bytes, number: int = 1) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(f"xl/worksheets/sheet{number}.xml").decode("utf-8")


def workbook_bytes(documents: Sequence[Mapping[str, Any]], **kwargs: Any) -> bytes:
    buffer = io.BytesIO()
    write_xlsx(buffer, documents, **kwargs)
    return buffer.getvalue()


# ------------------------------------------------------------------- workbook
def test_columns_put_metadata_first_and_keep_insertion_order() -> None:
    documents = [{"name": "Ada", "_id": "1"}, {"email": "a@b.c", "_rev": 2}]
    assert columns_for(documents) == ["_id", "_rev", "name", "email"]


def test_cell_value_types() -> None:
    assert cell_value(None) == ("s", "")
    assert cell_value(True) == ("b", "1")
    assert cell_value(3) == ("n", "3")
    assert cell_value(1.5) == ("n", "1.5")
    assert cell_value(float("nan"))[0] == "s"
    assert cell_value("hi") == ("s", "hi")
    assert cell_value(datetime(2024, 1, 2, 3, 4, 5)) == ("s", "2024-01-02T03:04:05")
    assert cell_value({"b": 1, "a": 2}) == ("s", '{"a": 2, "b": 1}')


def test_write_xlsx_produces_the_parts_excel_requires() -> None:
    data = workbook_bytes([{"_id": "1", "name": "Ada"}])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert set(archive.namelist()) == {
            "[Content_Types].xml",
            "_rels/.rels",
            "xl/workbook.xml",
            "xl/_rels/workbook.xml.rels",
            "xl/worksheets/sheet1.xml",
        }


def test_write_xlsx_writes_header_and_escapes_values() -> None:
    data = workbook_bytes([{"_id": "1", "name": "Ada & <Lovelace>", "count": 2, "ok": True}])
    sheet = sheet_xml(data)
    assert "Ada &amp; &lt;Lovelace&gt;" in sheet
    assert '<c r="A1" t="inlineStr"><is><t xml:space="preserve">_id</t></is></c>' in sheet
    assert '<c r="C2"><v>2</v></c>' in sheet
    assert '<c r="D2" t="b"><v>1</v></c>' in sheet


def test_write_xlsx_uses_explicit_columns_and_blanks_missing_keys() -> None:
    data = workbook_bytes([{"_id": "1"}], columns=["_id", "missing"])
    assert '<c r="B2" t="inlineStr"/>' in sheet_xml(data)


def test_write_xlsx_serialises_nested_values_as_json() -> None:
    data = workbook_bytes([{"_id": "1", "tags": ["a", "b"]}])
    assert "[&quot;a&quot;, &quot;b&quot;]" in sheet_xml(data)


def test_write_xlsx_strips_control_characters() -> None:
    data = workbook_bytes([{"_id": "1", "note": "a\x07b"}])
    assert '<t xml:space="preserve">ab</t>' in sheet_xml(data)


def test_column_names_pass_z() -> None:
    documents = [{f"c{index}": index for index in range(28)}]
    assert '<c r="AB1"' in sheet_xml(workbook_bytes(documents))


def test_write_workbook_sanitises_and_deduplicates_sheet_names() -> None:
    buffer = io.BytesIO()
    write_workbook(buffer, [("a/b", ["x"], [[1]]), ("a b", ["x"], [[2]])])
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        workbook = archive.read("xl/workbook.xml").decode("utf-8")
    assert 'name="a b"' in workbook and 'name="a b~2"' in workbook


def test_write_workbook_strips_control_characters_from_sheet_names() -> None:
    buffer = io.BytesIO()
    write_workbook(
        buffer,
        [
            ("bad\x00name", ["x"], [[1]]),
            ("bad\x01name", ["x"], [[2]]),
            ("bad\tname", ["x"], [[3]]),
            ("bad\nname", ["x"], [[4]]),
            ("bad\x7fname", ["x"], [[5]]),
            ("bad\x9fname", ["x"], [[6]]),
        ],
    )
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        workbook = archive.read("xl/workbook.xml").decode("utf-8")
    sheet_names = re.findall(r'<sheet name="([^"]*)"', workbook)
    control_chars = "\x00\x01\t\n\x7f\x9f"
    assert not any(character in name for name in sheet_names for character in control_chars)
    assert sheet_names == [
        "badname",
        "badname~2",
        "badname~3",
        "badname~4",
        "badname~5",
        "badname~6",
    ]


def test_write_workbook_rejects_an_empty_workbook() -> None:
    with pytest.raises(ValidationError):
        write_workbook(io.BytesIO(), [])


# --------------------------------------------------------------- normalisation
def test_normalize_value_converts_database_types() -> None:
    assert normalize_value(Decimal("1.50")) == "1.50"
    assert normalize_value(date(2024, 1, 2)) == "2024-01-02"
    assert normalize_value(UUID(int=1)) == "00000000-0000-0000-0000-000000000001"
    assert normalize_value(b"bytes") == base64.b64encode(b"bytes").decode("ascii")
    assert normalize_value(b"\xff") == base64.b64encode(b"\xff").decode("ascii")
    assert normalize_value({"a": Decimal("1")}) == {"a": "1"}
    assert normalize_value([Decimal("1"), None]) == ["1", None]
    assert normalize_value(float("inf")) == "inf"
    assert normalize_value(object()).startswith("<object")


def test_normalize_record_rejects_non_mappings() -> None:
    with pytest.raises(ValidationError):
        normalize_record(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_from_records_normalises_each_record() -> None:
    assert list(from_records([{"n": Decimal("2")}])) == [{"n": "2"}]


# ------------------------------------------------------------------ relational
@pytest.fixture
def sqlite_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT, score REAL)")
    connection.executemany(
        "INSERT INTO users VALUES (?, ?, ?)",
        [("1", "Ada", 9.5), ("2", "Grace", 8.0)],
    )
    connection.commit()
    yield connection
    connection.close()


def test_from_sql_yields_documents(sqlite_connection: sqlite3.Connection) -> None:
    rows = list(from_sql(sqlite_connection, "SELECT id, name, score FROM users ORDER BY id"))
    assert rows == [
        {"id": "1", "name": "Ada", "score": 9.5},
        {"id": "2", "name": "Grace", "score": 8.0},
    ]


def test_from_sql_passes_parameters(sqlite_connection: sqlite3.Connection) -> None:
    rows = list(from_sql(sqlite_connection, "SELECT name FROM users WHERE id = ?", ("2",)))
    assert rows == [{"name": "Grace"}]


def test_from_dbapi_reads_in_chunks(sqlite_connection: sqlite3.Connection) -> None:
    cursor = sqlite_connection.execute("SELECT id FROM users ORDER BY id")
    assert list(from_dbapi(cursor, fetch_size=1)) == [{"id": "1"}, {"id": "2"}]


def test_from_dbapi_accepts_mapping_rows() -> None:
    class MappingCursor:
        description = (("id", None),)

        def __init__(self) -> None:
            self._rows: List[Dict[str, Any]] = [{"id": 1, "when": date(2024, 1, 2)}]

        def fetchmany(self, size: int) -> List[Dict[str, Any]]:
            rows, self._rows = self._rows[:size], self._rows[size:]
            return rows

    assert list(from_dbapi(MappingCursor())) == [{"id": 1, "when": "2024-01-02"}]


def test_from_dbapi_disambiguates_duplicate_columns(sqlite_connection: sqlite3.Connection) -> None:
    cursor = sqlite_connection.execute("SELECT id, id FROM users WHERE id = '1'")
    assert list(from_dbapi(cursor)) == [{"id": "1", "id_1": "1"}]


def test_from_dbapi_disambiguates_columns_colliding_with_generated_suffixes() -> None:
    class FakeCursor:
        description = (("id_2", None), ("id", None), ("id", None))

        def __init__(self) -> None:
            self._rows: List[tuple[Any, ...]] = [("a", "b", "c")]

        def fetchmany(self, size: int) -> List[tuple[Any, ...]]:
            rows, self._rows = self._rows[:size], self._rows[size:]
            return rows

    documents = list(from_dbapi(FakeCursor()))
    assert documents == [{"id_2": "a", "id": "b", "id_3": "c"}]


def test_from_dbapi_requires_a_described_cursor(sqlite_connection: sqlite3.Connection) -> None:
    cursor = sqlite_connection.cursor()
    with pytest.raises(ValidationError):
        list(from_dbapi(cursor))


def test_from_dbapi_rejects_a_bad_fetch_size(sqlite_connection: sqlite3.Connection) -> None:
    cursor = sqlite_connection.execute("SELECT id FROM users")
    with pytest.raises(ValidationError):
        list(from_dbapi(cursor, fetch_size=0))


# -------------------------------------------------------------- non-relational
class FakeMongoCursor:
    def __init__(self, documents: Sequence[Mapping[str, Any]]) -> None:
        self.documents = list(documents)

    def limit(self, count: int) -> FakeMongoCursor:
        return FakeMongoCursor(self.documents[:count])

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        return iter(self.documents)


class FakeMongoCollection:
    def __init__(self, documents: Sequence[Mapping[str, Any]]) -> None:
        self.documents = list(documents)
        self.calls: List[Any] = []

    def find(
        self,
        filter: Mapping[str, Any],
        projection: Optional[Mapping[str, Any]] = None,
    ) -> FakeMongoCursor:
        self.calls.append((filter, projection))
        return FakeMongoCursor(self.documents)


def test_from_mongo_normalises_and_forwards_the_query() -> None:
    collection = FakeMongoCollection([{"_id": UUID(int=2), "name": "Ada"}])
    rows = list(from_mongo(collection, {"name": "Ada"}, projection={"name": 1}))
    assert rows == [{"_id": "00000000-0000-0000-0000-000000000002", "name": "Ada"}]
    assert collection.calls == [({"name": "Ada"}, {"name": 1})]


def test_from_mongo_applies_a_limit() -> None:
    collection = FakeMongoCollection([{"n": 1}, {"n": 2}])
    assert list(from_mongo(collection, limit=1)) == [{"n": 1}]


def test_from_mongo_rejects_objects_without_find() -> None:
    with pytest.raises(ValidationError):
        list(from_mongo(object()))


# ------------------------------------------------------------ client wiring
@responses.activate
def test_collection_export_excel_writes_every_document(db: GitDb) -> None:
    register_documents({"1": {"_id": "1", "name": "Ada"}, "2": {"_id": "2", "name": "Grace"}})
    buffer = io.BytesIO()

    columns = db.collection("users").export_excel(buffer)

    assert columns == ["_id", "name"]
    sheet = sheet_xml(buffer.getvalue())
    assert "Ada" in sheet and "Grace" in sheet


@responses.activate
def test_db_export_excel_writes_one_sheet_per_collection(db: GitDb) -> None:
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/contents/data",
        json=[{"name": "users", "type": "dir"}],
        status=200,
    )
    register_documents({"1": {"_id": "1", "name": "Ada"}})
    buffer = io.BytesIO()

    assert db.export_excel(buffer) == {"users": ["_id", "name"]}
    assert "Ada" in sheet_xml(buffer.getvalue())


@responses.activate
def test_db_export_excel_rejects_an_empty_selection(db: GitDb) -> None:
    with pytest.raises(ValidationError):
        db.export_excel(io.BytesIO(), collections=[])


@responses.activate
def test_import_records_writes_one_commit_per_chunk(db: GitDb) -> None:
    register_commit_endpoints(["blobA", "blobB"])
    register_commit_endpoints(["blobC"], base_commit="commit1", commit_sha="commit2")

    imported = db.collection("users").import_records(
        [
            {"_id": "1", "name": "Ada", "score": Decimal("9.5")},
            {"_id": "2", "name": "Grace"},
            {"name": "Katherine"},
        ],
        chunk_size=2,
    )

    assert imported == 3
    written = [decode(body["content"]) for body in _blob_bodies()]
    assert [document["_id"] for document in written[:2]] == ["1", "2"]
    assert written[0]["score"] == "9.5"
    assert written[2]["name"] == "Katherine"


@responses.activate
def test_import_records_discards_stale_generated_metadata(db: GitDb) -> None:
    register_commit_endpoints(["blobA"])

    db.collection("users").import_records(
        [
            {
                "_id": "1",
                "name": "Ada",
                "_rev": 41,
                "_created_at": "2000-01-01T00:00:00+00:00",
                "_updated_at": "2000-01-01T00:00:00+00:00",
            }
        ],
    )

    written = decode(_blob_bodies()[0]["content"])
    assert written["_rev"] == 1
    assert written["_created_at"] != "2000-01-01T00:00:00+00:00"
    assert written["_updated_at"] != "2000-01-01T00:00:00+00:00"


@responses.activate
def test_import_records_dry_run_writes_nothing(db: GitDb) -> None:
    imported = db.collection("users").import_records([{"_id": "1"}], dry_run=True)
    assert imported == 1
    assert list(responses.calls) == []


def test_import_records_rejects_a_bad_chunk_size(db: GitDb) -> None:
    with pytest.raises(ValidationError):
        db.collection("users").import_records([], chunk_size=0)


@responses.activate
def test_import_records_rejects_unusable_ids(db: GitDb) -> None:
    with pytest.raises(ValidationError):
        db.collection("users").import_records([{"_id": "../escape"}])


def _blob_bodies() -> List[Dict[str, Any]]:
    import json

    return [
        json.loads(call.request.body)
        for call in responses.calls
        if call.request.method == "POST" and call.request.url.endswith("/git/blobs")
    ]
