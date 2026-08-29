from __future__ import annotations

from typing import Any, Dict, List

import responses

from gitdb import GitDb
from tests.conftest import (
    API,
    REPO,
    blob_url,
    contents_payload,
    contents_url,
    register_documents,
    tree_payload,
    tree_url,
)

SCOPED_URL = tree_url("main:data/users")
FULL_TREE_URL = tree_url("main")


def _entries(paths: List[str], truncated: bool = False) -> Dict[str, Any]:
    return tree_payload(paths, truncated)


@responses.activate
def test_list_uses_path_scoped_tree(db: GitDb) -> None:
    responses.add(
        responses.GET,
        SCOPED_URL,
        json=_entries(["b.json", "a.json", "notes.md"]),
        status=200,
    )
    assert db.collection("users").list() == ["a", "b"]
    assert responses.calls[0].request.params["recursive"] == "1"
    # Only the collection subtree is fetched, never the whole repository tree.
    assert len(responses.calls) == 1


@responses.activate
def test_scoped_listing_includes_shard_directories() -> None:
    db = GitDb(REPO, token="t", shard_depth=1, shard_width=2)
    responses.add(responses.GET, SCOPED_URL, json=_entries(["ab/abcd.json"]), status=200)
    assert db.collection("users").list() == ["abcd"]


@responses.activate
def test_list_respects_limit_and_cursor(db: GitDb) -> None:
    responses.add(
        responses.GET,
        SCOPED_URL,
        json=_entries([f"{letter}.json" for letter in "abcde"]),
        status=200,
    )
    users = db.collection("users")
    assert users.list(limit=2) == ["a", "b"]
    assert users.list(limit=2, after="b") == ["c", "d"]
    assert users.list(after="e") == []


@responses.activate
def test_truncated_scoped_tree_descends_per_shard() -> None:
    db = GitDb(REPO, token="t", shard_depth=1, shard_width=2)
    responses.add(responses.GET, SCOPED_URL, json=_entries([], truncated=True), status=200)
    responses.add(
        responses.GET,
        SCOPED_URL,
        json={"sha": "t", "truncated": False, "tree": [{"path": "ab", "type": "tree"}]},
        status=200,
    )
    responses.add(
        responses.GET,
        tree_url("main:data/users/ab"),
        json=_entries(["abcd.json"]),
        status=200,
    )
    assert db.collection("users").list() == ["abcd"]


@responses.activate
def test_scoped_tree_falls_back_to_repository_tree(db: GitDb) -> None:
    responses.add(responses.GET, SCOPED_URL, json={"message": "Not Found"}, status=404)
    responses.add(
        responses.GET,
        FULL_TREE_URL,
        json=_entries(["data/users/a.json", "data/posts/z.json", "README.md"]),
        status=200,
    )
    assert db.collection("users").list() == ["a"]


@responses.activate
def test_truncated_repository_tree_falls_back_to_contents(db: GitDb) -> None:
    responses.add(responses.GET, SCOPED_URL, json={"message": "Not Found"}, status=404)
    responses.add(
        responses.GET, FULL_TREE_URL, json=_entries(["data/users/a.json"], True), status=200
    )
    responses.add(
        responses.GET,
        contents_url("data/users"),
        json=[
            {"path": "data/users/a.json", "type": "file", "sha": "b1"},
            {"path": "data/users/b.json", "type": "file", "sha": "b2"},
            {"path": "data/users/readme.md", "type": "file", "sha": "b3"},
        ],
        status=200,
    )
    assert db.collection("users").list() == ["a", "b"]


@responses.activate
def test_contents_fallback_recurses_into_shards() -> None:
    db = GitDb(REPO, token="t", shard_depth=1, shard_width=2)
    responses.add(responses.GET, SCOPED_URL, json={"message": "Not Found"}, status=404)
    responses.add(responses.GET, FULL_TREE_URL, json=_entries([], True), status=200)
    responses.add(
        responses.GET,
        contents_url("data/users"),
        json=[{"path": "data/users/ab", "type": "dir"}],
        status=200,
    )
    responses.add(
        responses.GET,
        contents_url("data/users/ab"),
        json=[{"path": "data/users/ab/abcd.json", "type": "file", "sha": "b1"}],
        status=200,
    )
    assert db.collection("users").list() == ["abcd"]


@responses.activate
def test_missing_collection_is_empty(db: GitDb) -> None:
    responses.add(responses.GET, SCOPED_URL, json={"message": "Not Found"}, status=404)
    responses.add(responses.GET, FULL_TREE_URL, json={"message": "Not Found"}, status=404)
    responses.add(
        responses.GET, contents_url("data/users"), json={"message": "Not Found"}, status=404
    )
    assert db.collection("users").list() == []
    assert db.collection("users").count() == 0


@responses.activate
def test_all_reads_documents_from_blobs(db: GitDb) -> None:
    register_documents(
        {
            "ada": {"_id": "ada", "name": "Ada"},
            "bob": {"_id": "bob", "name": "Bob"},
        }
    )
    users = db.collection("users")
    assert [doc["name"] for doc in users.all()] == ["Ada", "Bob"]
    # One listing plus one blob per document, and no Contents API call at all.
    assert len(responses.calls) == 3
    assert not [call for call in responses.calls if "/contents/" in call.request.url]


@responses.activate
def test_find_and_count(db: GitDb) -> None:
    register_documents(
        {
            "ada": {"_id": "ada", "name": "Ada"},
            "bob": {"_id": "bob", "name": "Bob"},
        }
    )
    users = db.collection("users")
    matches = users.find(lambda doc: doc["name"].startswith("A"))
    assert [doc["_id"] for doc in matches] == ["ada"]
    assert len(users) == 2


@responses.activate
def test_all_reuses_cached_documents_when_sha_is_unchanged(db: GitDb) -> None:
    register_documents({"ada": {"_id": "ada", "name": "Ada"}})
    users = db.collection("users")
    assert len(list(users.all())) == 1
    responses.add(
        responses.GET,
        tree_url("main:data/users"),
        json=tree_payload([("ada.json", "blob-ada")]),
        status=200,
    )
    assert [doc["name"] for doc in users.all()] == ["Ada"]
    # The second pass listed again but did not refetch the unchanged blob.
    assert len([call for call in responses.calls if "/git/blobs/" in call.request.url]) == 1


@responses.activate
def test_all_refetches_when_the_blob_sha_changed(db: GitDb) -> None:
    register_documents({"ada": {"_id": "ada", "name": "Ada"}})
    users = db.collection("users")
    assert [doc["name"] for doc in users.all()] == ["Ada"]
    responses.add(
        responses.GET,
        tree_url("main:data/users"),
        json=tree_payload([("ada.json", "blob-ada-2")]),
        status=200,
    )
    responses.add(responses.GET, blob_url("blob-ada-2"), json={"_id": "ada", "name": "Ada L"})
    assert [doc["name"] for doc in users.all()] == ["Ada L"]


@responses.activate
def test_concurrent_reads_return_every_document() -> None:
    db = GitDb(REPO, token="t", concurrency=4)
    register_documents(
        {letter: {"_id": letter, "n": index} for index, letter in enumerate("abcde")}
    )
    assert sorted(doc["_id"] for doc in db.collection("users").all()) == list("abcde")
    db.close()


@responses.activate
def test_pages_walk_the_collection_with_a_cursor(db: GitDb) -> None:
    register_documents({letter: {"_id": letter} for letter in "abc"})
    users = db.collection("users")
    first = users.page(2)
    assert [doc["_id"] for doc in first.documents] == ["a", "b"]
    assert first.cursor == "b"
    second = users.page(2, after=first.cursor)
    assert [doc["_id"] for doc in second.documents] == ["c"]
    assert second.cursor is None
    assert [[doc["_id"] for doc in page] for page in users.pages(2)] == [["a", "b"], ["c"]]


@responses.activate
def test_history(db: GitDb) -> None:
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/commits",
        json=[
            {
                "sha": "c2",
                "html_url": "https://github.com/owner/name/commit/c2",
                "commit": {
                    "message": "update users/ada",
                    "author": {"name": "Ada", "date": "2024-02-02T00:00:00Z"},
                },
            }
        ],
        status=200,
    )
    history = db.collection("users").history("ada")
    assert history == [
        {
            "sha": "c2",
            "message": "update users/ada",
            "author": "Ada",
            "date": "2024-02-02T00:00:00Z",
            "url": "https://github.com/owner/name/commit/c2",
        }
    ]
    assert responses.calls[0].request.params["path"] == "data/users/ada.json"


@responses.activate
def test_large_document_is_read_through_the_blob_api(db: GitDb) -> None:
    payload = contents_payload("data/users/big.json", {"_id": "big"})
    payload.update({"content": "", "encoding": "none", "size": 2_000_000, "sha": "bigblob"})
    responses.add(responses.GET, contents_url("data/users/big.json"), json=payload, status=200)
    responses.add(responses.GET, blob_url("bigblob"), json={"_id": "big", "name": "huge"})
    assert db.collection("users").get("big") == {"_id": "big", "name": "huge"}


@responses.activate
def test_collections_lists_directories_under_the_root(db: GitDb) -> None:
    responses.add(
        responses.GET,
        contents_url("data"),
        json=[
            {"name": "users", "type": "dir"},
            {"name": "notes", "type": "dir"},
            {"name": "_index", "type": "dir"},
            {"name": "_manifest", "type": "dir"},
            {"name": "README.md", "type": "file"},
        ],
        status=200,
    )
    assert db.collections() == ["notes", "users"]
    assert responses.calls[0].request.params["ref"] == "main"


@responses.activate
def test_collections_is_empty_when_the_root_is_missing(db: GitDb) -> None:
    responses.add(responses.GET, contents_url("data"), json={"message": "Not Found"}, status=404)
    assert db.collections() == []
