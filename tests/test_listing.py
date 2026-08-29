from __future__ import annotations

import responses

from gitdb import GitDb
from tests.conftest import API, REPO, contents_payload, contents_url

TREE_URL = f"{API}/repos/{REPO}/git/trees/main"


def _tree(paths: list[str], truncated: bool = False) -> dict:
    return {
        "sha": "tree0",
        "truncated": truncated,
        "tree": [{"path": path, "type": "blob", "sha": "b"} for path in paths],
    }


@responses.activate
def test_list_uses_trees_api(db: GitDb) -> None:
    responses.add(
        responses.GET,
        TREE_URL,
        json=_tree(
            [
                "data/users/b.json",
                "data/users/a.json",
                "data/users/notes.md",
                "data/posts/z.json",
                "README.md",
            ]
        ),
        status=200,
    )
    assert db.collection("users").list() == ["a", "b"]
    assert responses.calls[0].request.params["recursive"] == "1"


@responses.activate
def test_list_respects_limit(db: GitDb) -> None:
    responses.add(
        responses.GET,
        TREE_URL,
        json=_tree([f"data/users/{i}.json" for i in "abcde"]),
        status=200,
    )
    assert db.collection("users").list(limit=2) == ["a", "b"]


@responses.activate
def test_truncated_tree_falls_back_to_contents_listing(db: GitDb) -> None:
    responses.add(responses.GET, TREE_URL, json=_tree(["data/users/a.json"], True), status=200)
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
    responses.add(responses.GET, TREE_URL, json=_tree([], True), status=200)
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
def test_missing_tree_falls_back_and_missing_collection_is_empty(db: GitDb) -> None:
    responses.add(responses.GET, TREE_URL, json={"message": "Not Found"}, status=404)
    responses.add(
        responses.GET, contents_url("data/users"), json={"message": "Not Found"}, status=404
    )
    assert db.collection("users").list() == []
    assert db.collection("users").count() == 0


@responses.activate
def test_all_find_and_count(db: GitDb) -> None:
    responses.add(
        responses.GET,
        TREE_URL,
        json=_tree(["data/users/ada.json", "data/users/bob.json"]),
        status=200,
    )
    responses.add(
        responses.GET,
        contents_url("data/users/ada.json"),
        json=contents_payload("data/users/ada.json", {"_id": "ada", "name": "Ada"}),
        status=200,
    )
    responses.add(
        responses.GET,
        contents_url("data/users/bob.json"),
        json=contents_payload("data/users/bob.json", {"_id": "bob", "name": "Bob"}),
        status=200,
    )
    users = db.collection("users")
    assert [doc["name"] for doc in users.all()] == ["Ada", "Bob"]
    matches = users.find(lambda doc: doc["name"].startswith("A"))
    assert [doc["_id"] for doc in matches] == ["ada"]
    assert len(users) == 2


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
