from __future__ import annotations

import json

import pytest
import responses

from gitdb import AuthError, GitDb
from tests.conftest import API, REPO, decode


def _register_batch_endpoints(blob_shas: list[str]) -> None:
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
    for sha in blob_shas:
        responses.add(
            responses.POST, f"{API}/repos/{REPO}/git/blobs", json={"sha": sha}, status=201
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


@responses.activate
def test_batch_builds_tree_and_commit(db: GitDb) -> None:
    _register_batch_endpoints(["blobA", "blobB"])

    with db.batch(message="seed users") as batch:
        batch.put("users", "1", {"name": "Ada"})
        batch.put("users", "2", {"name": "Grace"})
        batch.delete("users", "3")

    blob_body = json.loads(responses.calls[2].request.body)
    assert decode(blob_body["content"])["_id"] == "1"
    assert blob_body["encoding"] == "base64"

    tree_body = json.loads(responses.calls[4].request.body)
    assert tree_body["base_tree"] == "tree0"
    assert tree_body["tree"] == [
        {"path": "data/users/1.json", "mode": "100644", "type": "blob", "sha": "blobA"},
        {"path": "data/users/2.json", "mode": "100644", "type": "blob", "sha": "blobB"},
        {"path": "data/users/3.json", "mode": "100644", "type": "blob", "sha": None},
    ]

    commit_body = json.loads(responses.calls[5].request.body)
    assert commit_body == {"message": "seed users", "tree": "tree1", "parents": ["commit0"]}

    ref_body = json.loads(responses.calls[6].request.body)
    assert ref_body == {"sha": "commit1", "force": False}


@responses.activate
def test_batch_commit_returns_sha_and_clears_queue(db: GitDb) -> None:
    _register_batch_endpoints(["blobA"])
    batch = db.batch("one")
    batch.put("users", "1", {"name": "Ada"})
    assert batch.operations == 1
    assert batch.commit() == "commit1"
    assert batch.operations == 0
    assert batch.commit() is None


@responses.activate
def test_batch_insert_generates_id(db: GitDb) -> None:
    _register_batch_endpoints(["blobA"])
    with db.batch("insert") as batch:
        doc_id = batch.insert("users", {"name": "Ada"})
    assert len(doc_id) == 26
    tree_body = json.loads(responses.calls[3].request.body)
    assert tree_body["tree"][0]["path"] == f"data/users/{doc_id}.json"


@responses.activate
def test_batch_is_discarded_on_exception(db: GitDb) -> None:
    with pytest.raises(RuntimeError):
        with db.batch("boom") as batch:
            batch.put("users", "1", {"name": "Ada"})
            raise RuntimeError("boom")
    assert len(responses.calls) == 0


@responses.activate
def test_empty_batch_makes_no_requests(db: GitDb) -> None:
    with db.batch("nothing"):
        pass
    assert len(responses.calls) == 0


def test_batch_put_supersedes_delete(db: GitDb) -> None:
    batch = db.batch("m")
    batch.delete("users", "1")
    batch.put("users", "1", {"name": "Ada"})
    assert batch.operations == 1
    batch.delete("users", "1")
    assert batch.operations == 1


def test_batch_rejected_in_read_only_mode() -> None:
    db = GitDb(REPO, read_only=True)
    with pytest.raises(AuthError):
        db.batch("nope")
