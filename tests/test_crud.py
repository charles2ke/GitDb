from __future__ import annotations

import json
import re
from typing import Any, Dict

import pytest
import responses

from gitdb import AuthError, ConflictError, GitDb, NotFoundError
from tests.conftest import API, REPO, contents_payload, contents_url, decode, encode


@responses.activate
def test_insert_creates_document_with_metadata(db: GitDb) -> None:
    responses.add(
        responses.PUT,
        contents_url("data/users/ada.json"),
        json={"content": {"sha": "blob1"}},
        status=201,
    )
    doc_id = db.collection("users").insert({"name": "Ada"}, id="ada")

    assert doc_id == "ada"
    body = json.loads(responses.calls[0].request.body)
    stored = decode(body["content"])
    assert stored["name"] == "Ada"
    assert stored["_id"] == "ada"
    assert stored["_rev"] == 1
    assert stored["_created_at"] and stored["_updated_at"]
    assert "sha" not in body
    assert body["branch"] == "main"


@responses.activate
def test_insert_generates_sortable_id(db: GitDb) -> None:
    responses.add(
        responses.PUT,
        re.compile(rf"{API}/repos/{REPO}/contents/data/users/.*\.json"),
        json={"content": {"sha": "blob1"}},
        status=201,
    )
    doc_id = db.collection("users").insert({"name": "Ada"})

    assert len(doc_id) == 26
    assert responses.calls[0].request.url.endswith(f"data/users/{doc_id}.json")


@responses.activate
def test_get_returns_none_for_missing_document(db: GitDb) -> None:
    responses.add(
        responses.GET,
        contents_url("data/users/nope.json"),
        json={"message": "Not Found"},
        status=404,
    )
    assert db.collection("users").get("nope") is None


@responses.activate
def test_get_returns_document(db: GitDb) -> None:
    document = {"_id": "ada", "name": "Ada", "_rev": 1}
    responses.add(
        responses.GET,
        contents_url("data/users/ada.json"),
        json=contents_payload("data/users/ada.json", document),
        status=200,
    )
    assert db.collection("users").get("ada") == document


@responses.activate
def test_update_merges_and_sends_sha(db: GitDb) -> None:
    existing: Dict[str, Any] = {
        "_id": "ada",
        "name": "Ada",
        "email": "old@example.com",
        "_rev": 1,
        "_created_at": "2024-01-01T00:00:00Z",
    }
    responses.add(
        responses.GET,
        contents_url("data/users/ada.json"),
        json=contents_payload("data/users/ada.json", existing, sha="blobA"),
        status=200,
    )
    responses.add(
        responses.PUT,
        contents_url("data/users/ada.json"),
        json={"content": {"sha": "blobB"}},
        status=200,
    )

    updated = db.collection("users").update("ada", {"email": "new@example.com"})

    body = json.loads(responses.calls[1].request.body)
    assert body["sha"] == "blobA"
    assert updated["email"] == "new@example.com"
    assert updated["name"] == "Ada"
    assert updated["_rev"] == 2
    assert updated["_created_at"] == "2024-01-01T00:00:00Z"


@responses.activate
def test_update_missing_document_raises(db: GitDb) -> None:
    responses.add(
        responses.GET,
        contents_url("data/users/ghost.json"),
        json={"message": "Not Found"},
        status=404,
    )
    with pytest.raises(NotFoundError):
        db.collection("users").update("ghost", {"a": 1})


@responses.activate
def test_replace_drops_old_fields(db: GitDb) -> None:
    existing = {"_id": "ada", "name": "Ada", "email": "old@example.com", "_rev": 3}
    responses.add(
        responses.GET,
        contents_url("data/users/ada.json"),
        json=contents_payload("data/users/ada.json", existing, sha="blobA"),
        status=200,
    )
    responses.add(
        responses.PUT,
        contents_url("data/users/ada.json"),
        json={"content": {"sha": "blobB"}},
        status=200,
    )
    result = db.collection("users").replace("ada", {"name": "Ada L"})
    assert "email" not in result
    assert result["_rev"] == 4


@responses.activate
def test_upsert_inserts_when_absent(db: GitDb) -> None:
    responses.add(
        responses.GET,
        contents_url("data/users/new.json"),
        json={"message": "Not Found"},
        status=404,
    )
    responses.add(
        responses.PUT,
        contents_url("data/users/new.json"),
        json={"content": {"sha": "blob1"}},
        status=201,
    )
    responses.add(
        responses.GET,
        contents_url("data/users/new.json"),
        json=contents_payload("data/users/new.json", {"_id": "new", "name": "N"}),
        status=200,
    )
    result = db.collection("users").upsert("new", {"name": "N"})
    assert result["name"] == "N"


@responses.activate
def test_delete_uses_sha(db: GitDb) -> None:
    responses.add(
        responses.GET,
        contents_url("data/users/ada.json"),
        json=contents_payload("data/users/ada.json", {"_id": "ada"}, sha="blobA"),
        status=200,
    )
    responses.add(
        responses.DELETE,
        contents_url("data/users/ada.json"),
        json={"commit": {"sha": "c1"}},
        status=200,
    )
    db.collection("users").delete("ada")
    body = json.loads(responses.calls[1].request.body)
    assert body["sha"] == "blobA"


@responses.activate
def test_delete_missing_raises(db: GitDb) -> None:
    responses.add(
        responses.GET,
        contents_url("data/users/ghost.json"),
        json={"message": "Not Found"},
        status=404,
    )
    with pytest.raises(NotFoundError):
        db.collection("users").delete("ghost")


@responses.activate
def test_insert_conflict_when_document_exists(db: GitDb) -> None:
    responses.add(
        responses.PUT,
        contents_url("data/users/ada.json"),
        json={"message": "Invalid request. sha wasn't supplied."},
        status=422,
    )
    with pytest.raises(ConflictError):
        db.collection("users").insert({"name": "Ada"}, id="ada")


@responses.activate
def test_update_retries_after_sha_conflict(db: GitDb) -> None:
    existing = {"_id": "ada", "name": "Ada", "_rev": 1}
    responses.add(
        responses.GET,
        contents_url("data/users/ada.json"),
        json=contents_payload("data/users/ada.json", existing, sha="stale"),
        status=200,
    )
    responses.add(
        responses.PUT,
        contents_url("data/users/ada.json"),
        json={"message": "does not match sha"},
        status=409,
    )
    responses.add(
        responses.GET,
        contents_url("data/users/ada.json"),
        json=contents_payload("data/users/ada.json", existing, sha="fresh"),
        status=200,
    )
    responses.add(
        responses.PUT,
        contents_url("data/users/ada.json"),
        json={"content": {"sha": "blobB"}},
        status=200,
    )

    db.collection("users").update("ada", {"name": "Ada L"})

    assert json.loads(responses.calls[1].request.body)["sha"] == "stale"
    assert json.loads(responses.calls[3].request.body)["sha"] == "fresh"


@responses.activate
def test_update_gives_up_after_conflict_retries() -> None:
    db = GitDb(REPO, token="t", conflict_retries=1)
    responses.add(
        responses.GET,
        contents_url("data/users/ada.json"),
        json=contents_payload("data/users/ada.json", {"_id": "ada"}, sha="s"),
        status=200,
    )
    responses.add(
        responses.PUT,
        contents_url("data/users/ada.json"),
        json={"message": "sha mismatch"},
        status=409,
    )
    with pytest.raises(ConflictError):
        db.collection("users").update("ada", {"a": 1})


@responses.activate
def test_read_only_uses_raw_endpoint_and_blocks_writes() -> None:
    db = GitDb(REPO, read_only=True)
    responses.add(
        responses.GET,
        f"https://raw.githubusercontent.com/{REPO}/main/data/users/ada.json",
        json={"_id": "ada", "name": "Ada"},
        status=200,
    )
    assert db.collection("users").get("ada") == {"_id": "ada", "name": "Ada"}
    with pytest.raises(AuthError):
        db.collection("users").insert({"name": "x"}, id="x")


@responses.activate
def test_enterprise_api_url() -> None:
    db = GitDb(REPO, token="t", api_url="https://ghe.example.com/api/v3")
    responses.add(
        responses.GET,
        "https://ghe.example.com/api/v3/repos/owner/name/contents/data/users/ada.json",
        json=contents_payload("data/users/ada.json", {"_id": "ada"}),
        status=200,
    )
    assert db.collection("users").get("ada") == {"_id": "ada"}


@responses.activate
def test_sha_cache_and_invalidate(db: GitDb) -> None:
    responses.add(
        responses.GET,
        contents_url("data/users/ada.json"),
        json=contents_payload("data/users/ada.json", {"_id": "ada"}, sha="blobA"),
        status=200,
    )
    responses.add(
        responses.DELETE,
        contents_url("data/users/ada.json"),
        json={"commit": {"sha": "c1"}},
        status=200,
    )
    assert db.collection("users").get("ada") is not None
    db.collection("users").delete("ada")
    # A single GET was enough: the delete reused the cached sha.
    assert sum(1 for call in responses.calls if call.request.method == "GET") == 1
    db.invalidate()
    assert db._sha_cache == {}


def test_encode_decode_roundtrip() -> None:
    assert decode(encode({"a": 1})) == {"a": 1}
