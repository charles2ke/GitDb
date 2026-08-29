from __future__ import annotations

import json
from typing import Any, Dict, List

import responses

from gitdb import GitDb
from gitdb.derived import (
    apply_index,
    apply_manifest,
    build_index,
    empty_index,
    index_key,
    index_values,
    lookup_index,
    manifest_documents,
    manifest_ids,
)
from tests.conftest import (
    API,
    REPO,
    blob_url,
    contents_payload,
    contents_url,
    decode,
    register_commit_endpoints,
    register_documents,
    tree_url,
)

INDEX_PATH = "data/_index/users/email.json"
MANIFEST_PATH = "data/_manifest/users.json"


def indexed_db(**kwargs: Any) -> GitDb:
    return GitDb(REPO, token="t", indexes={"users": ["email"]}, **kwargs)


def blob_bodies() -> List[Dict[str, Any]]:
    return [
        decode(json.loads(call.request.body)["content"])
        for call in responses.calls
        if call.request.url.endswith("/git/blobs") and call.request.method == "POST"
    ]


# ------------------------------------------------------------------ pure logic
def test_index_key_normalises_values() -> None:
    assert index_key("ada@example.com") == "ada@example.com"
    assert index_key(7) == "7"
    assert index_key(None) == "null"
    assert index_key(True) == "true"


def test_index_values_handles_lists_and_missing_fields() -> None:
    assert index_values({"tags": ["a", "b", "a"]}, "tags") == ["a", "b"]
    assert index_values({"email": "a@b.c"}, "email") == ["a@b.c"]
    assert index_values({}, "email") == []


def test_apply_index_adds_moves_and_deletes() -> None:
    index = apply_index(empty_index("users", "email"), "users", "email", {"ada": {"email": "a@x"}})
    assert lookup_index(index, "a@x") == ["ada"]

    index = apply_index(index, "users", "email", {"ada": {"email": "b@x"}})
    assert lookup_index(index, "a@x") == []
    assert lookup_index(index, "b@x") == ["ada"]

    index = apply_index(index, "users", "email", {"ada": None})
    assert lookup_index(index, "b@x") == []
    assert index["values"] == {}


def test_build_index_groups_documents_by_value() -> None:
    index = build_index(
        "users",
        "team",
        {"ada": {"team": "core"}, "bob": {"team": "core"}, "cy": {"team": "ops"}},
    )
    assert lookup_index(index, "core") == ["ada", "bob"]
    assert lookup_index(index, "ops") == ["cy"]


def test_manifest_tracks_ids_and_projections() -> None:
    manifest = apply_manifest({}, "users", {"ada": {"name": "Ada", "team": "core"}}, ["name"])
    assert manifest_ids(manifest) == ["ada"]
    assert manifest_documents(manifest) == {"ada": {"name": "Ada"}}
    manifest = apply_manifest(manifest, "users", {"ada": None}, ["name"])
    assert manifest_ids(manifest) == []
    assert manifest["count"] == 0


# ------------------------------------------------------------------- behaviour
@responses.activate
def test_indexed_insert_writes_the_index_in_the_same_commit() -> None:
    db = indexed_db()
    responses.add(
        responses.GET, contents_url(INDEX_PATH), json={"message": "Not Found"}, status=404
    )
    responses.add(
        responses.GET,
        contents_url("data/users/ada.json"),
        json={"message": "Not Found"},
        status=404,
    )
    register_commit_endpoints(["blobIndex", "blobDoc"])

    db.collection("users").insert({"email": "ada@example.com"}, id="ada")

    written = blob_bodies()
    assert len(written) == 2
    index = next(body for body in written if body.get("_index") == "email")
    assert lookup_index(index, "ada@example.com") == ["ada"]
    tree = next(
        json.loads(call.request.body)
        for call in responses.calls
        if call.request.url.endswith("/git/trees")
    )
    assert sorted(entry["path"] for entry in tree["tree"]) == [
        "data/_index/users/email.json",
        "data/users/ada.json",
    ]


@responses.activate
def test_indexed_insert_refuses_to_overwrite() -> None:
    db = indexed_db()
    responses.add(
        responses.GET, contents_url(INDEX_PATH), json={"message": "Not Found"}, status=404
    )
    responses.add(
        responses.GET,
        contents_url("data/users/ada.json"),
        json=contents_payload("data/users/ada.json", {"_id": "ada"}),
        status=200,
    )
    try:
        db.collection("users").insert({"email": "a@x"}, id="ada")
    except Exception as exc:  # noqa: BLE001 - asserting the type below
        assert type(exc).__name__ == "ConflictError"
    else:  # pragma: no cover - the insert must fail
        raise AssertionError("insert should have raised")
    assert not [call for call in responses.calls if call.request.url.endswith("/git/blobs")]


@responses.activate
def test_find_by_uses_the_index() -> None:
    db = indexed_db()
    index = build_index("users", "email", {"ada": {"email": "ada@example.com"}})
    responses.add(
        responses.GET,
        contents_url(INDEX_PATH),
        json=contents_payload(INDEX_PATH, index),
        status=200,
    )
    responses.add(
        responses.GET,
        contents_url("data/users/ada.json"),
        json=contents_payload("data/users/ada.json", {"_id": "ada", "email": "ada@example.com"}),
        status=200,
    )
    matches = db.collection("users").find_by("email", "ada@example.com")
    assert [doc["_id"] for doc in matches] == ["ada"]
    # One request for the index and one for the document: no collection listing.
    assert len(responses.calls) == 2
    assert not [call for call in responses.calls if "/git/trees/" in call.request.url]


@responses.activate
def test_find_by_on_a_missing_index_returns_nothing() -> None:
    db = indexed_db()
    responses.add(
        responses.GET, contents_url(INDEX_PATH), json={"message": "Not Found"}, status=404
    )
    assert db.collection("users").find_by("email", "ada@example.com") == []


@responses.activate
def test_find_by_falls_back_to_a_scan_for_unindexed_fields(db: GitDb) -> None:
    register_documents({"ada": {"_id": "ada", "name": "Ada"}, "bob": {"_id": "bob", "name": "Bob"}})
    assert [doc["_id"] for doc in db.collection("users").find_by("name", "Bob")] == ["bob"]


@responses.activate
def test_reindex_rebuilds_from_the_documents() -> None:
    db = indexed_db()
    register_documents(
        {
            "ada": {"_id": "ada", "email": "ada@example.com"},
            "bob": {"_id": "bob", "email": "bob@example.com"},
        }
    )
    register_commit_endpoints(["blobIndex"])
    assert db.collection("users").reindex() == "commit1"
    index = blob_bodies()[0]
    assert lookup_index(index, "ada@example.com") == ["ada"]
    assert lookup_index(index, "bob@example.com") == ["bob"]


@responses.activate
def test_reindex_without_derived_files_is_a_no_op(db: GitDb) -> None:
    assert db.collection("users").reindex() is None
    assert len(responses.calls) == 0


@responses.activate
def test_manifest_backs_list_and_count() -> None:
    db = GitDb(REPO, token="t", manifests={"users": ["name"]})
    manifest = apply_manifest(
        {}, "users", {"ada": {"name": "Ada"}, "bob": {"name": "Bob"}}, ["name"]
    )
    responses.add(
        responses.GET,
        contents_url(MANIFEST_PATH),
        json=contents_payload(MANIFEST_PATH, manifest),
        status=200,
    )
    users = db.collection("users")
    assert users.list() == ["ada", "bob"]
    assert users.count() == 2
    assert not [call for call in responses.calls if "/git/trees/" in call.request.url]


@responses.activate
def test_manifest_is_updated_on_delete() -> None:
    db = GitDb(REPO, token="t", manifests={"users": ["name"]})
    manifest = apply_manifest({}, "users", {"ada": {"name": "Ada"}}, ["name"])
    responses.add(
        responses.GET,
        contents_url(MANIFEST_PATH),
        json=contents_payload(MANIFEST_PATH, manifest),
        status=200,
    )
    responses.add(
        responses.GET,
        contents_url("data/users/ada.json"),
        json=contents_payload("data/users/ada.json", {"_id": "ada", "name": "Ada"}),
        status=200,
    )
    register_commit_endpoints(["blobManifest"])
    db.collection("users").delete("ada")

    written = blob_bodies()[0]
    assert manifest_ids(written) == []
    tree = next(
        json.loads(call.request.body)
        for call in responses.calls
        if call.request.url.endswith("/git/trees")
    )
    entries = {entry["path"]: entry["sha"] for entry in tree["tree"]}
    assert entries["data/users/ada.json"] is None
    assert entries[MANIFEST_PATH] == "blobManifest"


@responses.activate
def test_index_files_are_not_listed_as_documents() -> None:
    db = indexed_db()
    responses.add(
        responses.GET,
        tree_url("main:data/users"),
        json={
            "sha": "t",
            "truncated": False,
            "tree": [{"path": "ada.json", "type": "blob", "sha": "b1"}],
        },
        status=200,
    )
    responses.add(responses.GET, blob_url("b1"), json={"_id": "ada"})
    assert db.collection("users").list() == ["ada"]
    assert db.index_path("users", "email") == INDEX_PATH
    assert db.manifest_path("users") == MANIFEST_PATH
    assert not db.index_path("users", "email").startswith(db.collection_path("users"))


@responses.activate
def test_batched_writes_update_derived_files_once() -> None:
    db = indexed_db()
    responses.add(
        responses.GET, contents_url(INDEX_PATH), json={"message": "Not Found"}, status=404
    )
    register_commit_endpoints(["blobA", "blobB", "blobIndex"])
    with db.batch("seed") as batch:
        batch.put("users", "ada", {"email": "ada@example.com"})
        batch.put("users", "bob", {"email": "bob@example.com"})

    index = next(body for body in blob_bodies() if body.get("_index") == "email")
    assert lookup_index(index, "ada@example.com") == ["ada"]
    assert lookup_index(index, "bob@example.com") == ["bob"]
    assert len([call for call in responses.calls if call.request.url.endswith("/git/commits")]) == 1
    assert responses.calls[-1].request.url == f"{API}/repos/{REPO}/git/refs/heads/main"
