from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest
import responses

from gitdb import CacheEntry, GitDb, MemoryCache, NullCache, ValidationError
from tests.conftest import (
    API,
    RAW,
    REPO,
    blob_url,
    contents_payload,
    contents_url,
    encode,
    register_documents,
    tree_url,
)

ADA = "data/users/ada.json"


def json_text(doc_id: str) -> str:
    return json.dumps({"_id": doc_id})


@responses.activate
def test_etag_is_sent_and_304_reuses_the_cached_document(db: GitDb) -> None:
    responses.add(
        responses.GET,
        contents_url(ADA),
        json=contents_payload(ADA, {"_id": "ada", "name": "Ada"}),
        status=200,
        headers={"ETag": 'W/"abc"'},
    )
    responses.add(responses.GET, contents_url(ADA), body="", status=304)

    users = db.collection("users")
    assert users.get("ada") == {"_id": "ada", "name": "Ada"}
    assert users.get("ada", fresh=True) == {"_id": "ada", "name": "Ada"}
    assert responses.calls[1].request.headers["If-None-Match"] == 'W/"abc"'
    assert "If-None-Match" not in responses.calls[0].request.headers


@responses.activate
def test_a_custom_cache_can_be_plugged_in() -> None:
    cache = MemoryCache()
    db = GitDb(REPO, token="t", cache=cache)
    responses.add(
        responses.GET,
        contents_url(ADA),
        json=contents_payload(ADA, {"_id": "ada"}, sha="s1"),
        status=200,
        headers={"ETag": 'W/"abc"'},
    )
    db.collection("users").get("ada")
    entry = cache.get(ADA)
    assert entry is not None
    assert (entry.sha, entry.etag) == ("s1", 'W/"abc"')

    # A second client sharing the same cache starts warm.
    other = GitDb(REPO, token="t", cache=cache)
    assert other._sha_cache[ADA] == "s1"


@responses.activate
def test_cache_false_never_stores_anything() -> None:
    db = GitDb(REPO, token="t", cache=False)
    assert isinstance(db.cache, NullCache)
    responses.add(
        responses.GET, contents_url(ADA), json=contents_payload(ADA, {"_id": "ada"}), status=200
    )
    responses.add(
        responses.GET, contents_url(ADA), json=contents_payload(ADA, {"_id": "ada"}), status=200
    )
    db.collection("users").get("ada")
    db.collection("users").get("ada")
    assert db._sha_cache == {}
    assert len(responses.calls) == 2
    assert "If-None-Match" not in responses.calls[1].request.headers


def test_cache_entry_merge_drops_a_stale_body() -> None:
    entry = CacheEntry(sha="a", etag='W/"1"', document={"_id": "ada"})
    same = entry.merge(CacheEntry(sha="a"))
    assert same.document == {"_id": "ada"}
    moved = entry.merge(CacheEntry(sha="b"))
    assert (moved.sha, moved.etag, moved.document) == ("b", None, None)


@responses.activate
def test_read_only_pins_raw_urls_to_a_commit() -> None:
    db = GitDb(REPO, read_only=True)
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/git/ref/heads/main",
        json={"object": {"sha": "commit7"}},
        status=200,
    )
    responses.add(
        responses.GET, f"{RAW}/{REPO}/commit7/{ADA}", json={"_id": "ada", "name": "Ada"}, status=200
    )
    assert db.collection("users").get("ada") == {"_id": "ada", "name": "Ada"}
    # The branch head is resolved once and reused for later reads.
    responses.add(
        responses.GET, f"{RAW}/{REPO}/commit7/{ADA}", json={"_id": "ada", "name": "Ada"}, status=200
    )
    db.collection("users").get("ada")
    heads = [call for call in responses.calls if "/git/ref/" in call.request.url]
    assert len(heads) == 1


@responses.activate
def test_pin_ref_false_reads_the_branch_name() -> None:
    db = GitDb(REPO, read_only=True, pin_ref=False)
    responses.add(responses.GET, f"{RAW}/{REPO}/main/{ADA}", json={"_id": "ada"}, status=200)
    assert db.collection("users").get("ada") == {"_id": "ada"}
    assert not [call for call in responses.calls if "/git/ref/" in call.request.url]


@responses.activate
def test_raw_reads_fall_back_to_the_branch_when_the_head_is_unreachable() -> None:
    db = GitDb(REPO, read_only=True)
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/git/ref/heads/main",
        json={"message": "Not Found"},
        status=404,
    )
    responses.add(responses.GET, f"{RAW}/{REPO}/main/{ADA}", json={"_id": "ada"}, status=200)
    assert db.collection("users").get("ada") == {"_id": "ada"}


@responses.activate
def test_raw_reads_revalidate_with_an_etag() -> None:
    db = GitDb(REPO, read_only=True, pin_ref=False)
    responses.add(
        responses.GET,
        f"{RAW}/{REPO}/main/{ADA}",
        json={"_id": "ada"},
        status=200,
        headers={"ETag": 'W/"r1"'},
    )
    responses.add(responses.GET, f"{RAW}/{REPO}/main/{ADA}", body="", status=304)
    users = db.collection("users")
    assert users.get("ada") == {"_id": "ada"}
    assert users.get("ada") == {"_id": "ada"}
    assert responses.calls[1].request.headers["If-None-Match"] == 'W/"r1"'


@responses.activate
def test_read_only_can_demand_a_fresh_read_from_the_api() -> None:
    db = GitDb(REPO, read_only=True, pin_ref=False)
    responses.add(
        responses.GET,
        contents_url(ADA),
        json=contents_payload(ADA, {"_id": "ada", "name": "fresh"}),
        status=200,
    )
    assert db.collection("users").get("ada", fresh=True)["name"] == "fresh"
    assert "raw.githubusercontent" not in responses.calls[0].request.url


@responses.activate
def test_at_pins_reads_to_a_commit(db: GitDb) -> None:
    pinned = db.at("commit7")
    assert pinned.read_only is True
    responses.add(
        responses.GET, f"{RAW}/{REPO}/commit7/{ADA}", json={"_id": "ada", "name": "old"}, status=200
    )
    assert pinned.collection("users").get("ada")["name"] == "old"
    with pytest.raises(ValidationError):
        db.at("")


@responses.activate
def test_snapshot_resolves_the_head_once(db: GitDb) -> None:
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/git/ref/heads/main",
        json={"object": {"sha": "commit9"}},
        status=200,
    )
    snapshot = db.snapshot()
    assert snapshot.ref == "commit9"
    assert snapshot.resolve_ref() == "commit9"
    assert len([call for call in responses.calls if "/git/ref/" in call.request.url]) == 1


@responses.activate
def test_a_view_does_not_reuse_the_parent_cache(db: GitDb) -> None:
    responses.add(
        responses.GET,
        contents_url(ADA),
        json=contents_payload(ADA, {"_id": "ada", "name": "head"}),
        status=200,
    )
    assert db.collection("users").get("ada")["name"] == "head"
    responses.add(
        responses.GET, f"{RAW}/{REPO}/commit7/{ADA}", json={"_id": "ada", "name": "old"}, status=200
    )
    assert db.at("commit7").collection("users").get("ada")["name"] == "old"


@responses.activate
def test_documents_larger_than_the_contents_cap_are_written_as_blobs() -> None:
    db = GitDb(REPO, token="t", contents_max_bytes=100)
    responses.add(responses.POST, f"{API}/repos/{REPO}/git/blobs", json={"sha": "big"}, status=201)
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/git/ref/heads/main",
        json={"object": {"sha": "commit0"}},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/git/commits/commit0",
        json={"tree": {"sha": "tree0"}},
        status=200,
    )
    responses.add(
        responses.POST, f"{API}/repos/{REPO}/git/trees", json={"sha": "tree1"}, status=201
    )
    responses.add(
        responses.POST, f"{API}/repos/{REPO}/git/commits", json={"sha": "commit1"}, status=201
    )
    responses.add(
        responses.PATCH,
        f"{API}/repos/{REPO}/git/refs/heads/main",
        json={"object": {"sha": "commit1"}},
        status=200,
    )
    assert db.collection("users").insert({"blob": "x" * 500}, id="big") == "big"
    # The Contents API was never used, so the ~1 MB limit does not apply.
    assert not [call for call in responses.calls if "/contents/" in call.request.url]


@responses.activate
def test_small_documents_still_use_the_contents_api(db: GitDb) -> None:
    responses.add(responses.GET, contents_url(ADA), json={"message": "Not Found"}, status=404)
    responses.add(responses.PUT, contents_url(ADA), json={"content": {"sha": "s1"}}, status=201)
    db.collection("users").insert({"name": "Ada"}, id="ada")
    assert responses.calls[-1].request.url.startswith(contents_url(ADA))


@responses.activate
def test_oversized_contents_response_falls_back_to_the_blob(db: GitDb) -> None:
    payload: Dict[str, Any] = contents_payload(ADA, {"_id": "ada"})
    payload.update({"content": "", "encoding": "none", "sha": "bigblob", "size": 3_000_000})
    responses.add(responses.GET, contents_url(ADA), json=payload, status=200)
    responses.add(responses.GET, blob_url("bigblob"), json={"_id": "ada", "name": "huge"})
    assert db.collection("users").get("ada")["name"] == "huge"
    assert responses.calls[1].request.headers["Accept"] == "application/vnd.github.raw"


@responses.activate
def test_blob_reads_tolerate_a_json_envelope(db: GitDb) -> None:
    payload: Dict[str, Any] = contents_payload(ADA, {"_id": "ada"})
    payload.update({"content": "", "encoding": "none", "sha": "bigblob"})
    responses.add(responses.GET, contents_url(ADA), json=payload, status=200)
    responses.add(
        responses.GET,
        blob_url("bigblob"),
        json={"sha": "bigblob", "encoding": "base64", "content": encode({"_id": "ada", "n": 1})},
    )
    assert db.collection("users").get("ada") == {"_id": "ada", "n": 1}


@responses.activate
def test_unreadable_contents_payload_is_rejected(db: GitDb) -> None:
    responses.add(
        responses.GET,
        contents_url(ADA),
        json={"path": ADA, "encoding": "none", "content": ""},
        status=200,
    )
    with pytest.raises(ValidationError):
        db.collection("users").get("ada")


@responses.activate
def test_graphql_bulk_fetch_collapses_reads() -> None:
    db = GitDb(REPO, token="t", use_graphql=True)
    responses.add(
        responses.GET,
        tree_url("main:data/users"),
        json={
            "sha": "t",
            "truncated": False,
            "tree": [
                {"path": "ada.json", "type": "blob", "sha": "b1"},
                {"path": "bob.json", "type": "blob", "sha": "b2"},
            ],
        },
        status=200,
    )
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={
            "data": {
                "repository": {
                    "d0": {"text": '{"_id": "ada"}', "oid": "b1"},
                    "d1": {"text": '{"_id": "bob"}', "oid": "b2"},
                }
            }
        },
        status=200,
    )
    assert [doc["_id"] for doc in db.collection("users").all()] == ["ada", "bob"]
    graphql_calls = [call for call in responses.calls if call.request.url.endswith("/graphql")]
    assert len(graphql_calls) == 1
    assert not [call for call in responses.calls if "/git/blobs/" in call.request.url]


@responses.activate
def test_graphql_batches_are_chunked() -> None:
    db = GitDb(REPO, token="t", use_graphql=True, graphql_batch_size=1)
    ids: List[str] = ["ada", "bob"]
    responses.add(
        responses.GET,
        tree_url("main:data/users"),
        json={
            "sha": "t",
            "truncated": False,
            "tree": [{"path": f"{doc_id}.json", "type": "blob", "sha": doc_id} for doc_id in ids],
        },
        status=200,
    )
    for doc_id in ids:
        responses.add(
            responses.POST,
            "https://api.github.com/graphql",
            json={"data": {"repository": {"d0": {"text": json_text(doc_id), "oid": doc_id}}}},
            status=200,
        )
    assert [doc["_id"] for doc in db.collection("users").all()] == ids
    assert len([call for call in responses.calls if call.request.url.endswith("/graphql")]) == 2


@responses.activate
def test_graphql_misses_fall_back_to_blobs() -> None:
    db = GitDb(REPO, token="t", use_graphql=True)
    register_documents({"ada": {"_id": "ada", "name": "Ada"}})
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={"data": {"repository": {"d0": None}}},
        status=200,
    )
    assert [doc["name"] for doc in db.collection("users").all()] == ["Ada"]
    assert [call for call in responses.calls if "/git/blobs/" in call.request.url]
