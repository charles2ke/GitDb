from __future__ import annotations

import json

import pytest
import responses

from gitdb import AuthError, ConflictError, GitDb
from tests.conftest import (
    API,
    REPO,
    contents_payload,
    contents_url,
    decode,
    register_commit_endpoints,
)


@responses.activate
def test_batch_builds_tree_and_commit(db: GitDb) -> None:
    register_commit_endpoints(["blobA", "blobB"])

    with db.batch(message="seed users") as batch:
        batch.put("users", "1", {"name": "Ada"})
        batch.put("users", "2", {"name": "Grace"})
        batch.delete("users", "3")

    blob_body = json.loads(responses.calls[0].request.body)
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
    register_commit_endpoints(["blobA"])
    batch = db.batch("one")
    batch.put("users", "1", {"name": "Ada"})
    assert batch.operations == 1
    assert batch.commit() == "commit1"
    assert batch.operations == 0
    assert batch.commit() is None


@responses.activate
def test_batch_insert_generates_id(db: GitDb) -> None:
    register_commit_endpoints(["blobA"])
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


@responses.activate
def test_batch_retries_when_the_branch_moved(db: GitDb) -> None:
    responses.add(
        responses.POST, f"{API}/repos/{REPO}/git/blobs", json={"sha": "blobA"}, status=201
    )
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
        json={"message": "Update is not a fast forward"},
        status=422,
    )
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/git/ref/heads/main",
        json={"object": {"sha": "commit9"}},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/git/commits/commit9",
        json={"tree": {"sha": "tree9"}},
        status=200,
    )
    responses.add(
        responses.POST, f"{API}/repos/{REPO}/git/trees", json={"sha": "tree2"}, status=201
    )
    responses.add(
        responses.POST, f"{API}/repos/{REPO}/git/commits", json={"sha": "commit2"}, status=201
    )
    responses.add(
        responses.PATCH,
        f"{API}/repos/{REPO}/git/refs/heads/main",
        json={"object": {"sha": "commit2"}},
        status=200,
    )

    batch = db.batch("retry me")
    batch.put("users", "1", {"name": "Ada"})
    assert batch.commit() == "commit2"

    # The blob was uploaded once and the tree was rebuilt on the new head.
    blob_calls = [call for call in responses.calls if call.request.url.endswith("/git/blobs")]
    assert len(blob_calls) == 1
    trees = [
        json.loads(call.request.body)
        for call in responses.calls
        if call.request.url.endswith("/git/trees")
    ]
    assert [tree["base_tree"] for tree in trees] == ["tree0", "tree9"]


@responses.activate
def test_batch_gives_up_after_batch_retries() -> None:
    db = GitDb(REPO, token="t", batch_retries=0)
    responses.add(
        responses.POST, f"{API}/repos/{REPO}/git/blobs", json={"sha": "blobA"}, status=201
    )
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
        json={"message": "Update is not a fast forward"},
        status=422,
    )
    batch = db.batch("nope")
    batch.put("users", "1", {"name": "Ada"})
    with pytest.raises(ConflictError):
        batch.commit()


@responses.activate
def test_batch_precondition_blocks_a_stale_write(db: GitDb) -> None:
    responses.add(
        responses.GET,
        contents_url("data/users/ada.json"),
        json=contents_payload("data/users/ada.json", {"_id": "ada"}, sha="fresh"),
        status=200,
    )
    batch = db.batch("guarded")
    batch.put("users", "ada", {"name": "Ada"}, expected_sha="stale")
    assert batch.expectations == 1
    with pytest.raises(ConflictError):
        batch.commit()


@responses.activate
def test_batch_precondition_on_revision(db: GitDb) -> None:
    responses.add(
        responses.GET,
        contents_url("data/users/ada.json"),
        json=contents_payload("data/users/ada.json", {"_id": "ada", "_rev": 7}, sha="s"),
        status=200,
    )
    batch = db.batch("guarded")
    batch.put("users", "ada", {"name": "Ada"}, expected_rev=3)
    with pytest.raises(ConflictError):
        batch.commit()


@responses.activate
def test_batch_absent_precondition_and_success(db: GitDb) -> None:
    responses.add(
        responses.GET,
        contents_url("data/users/ada.json"),
        json={"message": "Not Found"},
        status=404,
    )
    register_commit_endpoints(["blobA"])
    batch = db.batch("guarded")
    batch.put("users", "ada", {"name": "Ada"}, absent=True)
    assert batch.commit() == "commit1"


@responses.activate
def test_writer_flushes_every_n_operations(db: GitDb) -> None:
    register_commit_endpoints(["blobA", "blobB"])
    register_commit_endpoints(["blobC"], base_commit="commit1", commit_sha="commit2")
    with db.writer("bulk", max_operations=2) as writer:
        writer.put("users", "1", {"name": "Ada"})
        writer.put("users", "2", {"name": "Grace"})
        assert writer.pending == 0
        writer.put("users", "3", {"name": "Alan"})
        assert writer.pending == 1
    assert writer.commits == ["commit1", "commit2"]


@responses.activate
def test_writer_discards_pending_work_on_error(db: GitDb) -> None:
    with pytest.raises(RuntimeError):
        with db.writer("bulk", max_operations=10) as writer:
            writer.put("users", "1", {"name": "Ada"})
            raise RuntimeError("boom")
    assert writer.pending == 0
    assert len(responses.calls) == 0


@responses.activate
def test_transaction_fast_forwards_the_target_branch(db: GitDb) -> None:
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/git/ref/heads/main",
        json={"object": {"sha": "commit0"}},
        status=200,
    )
    responses.add(responses.POST, f"{API}/repos/{REPO}/git/refs", json={}, status=201)
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/git/ref/heads/work",
        json={"object": {"sha": "commit5"}},
        status=200,
    )
    responses.add(
        responses.PATCH,
        f"{API}/repos/{REPO}/git/refs/heads/main",
        json={"object": {"sha": "commit5"}},
        status=200,
    )
    responses.add(responses.DELETE, f"{API}/repos/{REPO}/git/refs/heads/work", body="", status=204)

    with db.transaction(branch="work") as tx:
        assert tx.view.branch == "work"
    assert tx.head_sha == "commit5"

    created = json.loads(responses.calls[1].request.body)
    assert created == {"ref": "refs/heads/work", "sha": "commit0"}
    published = json.loads(responses.calls[3].request.body)
    assert published == {"sha": "commit5", "force": False}
    assert responses.calls[4].request.method == "DELETE"


@responses.activate
def test_transaction_rolls_back_on_error(db: GitDb) -> None:
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/git/ref/heads/main",
        json={"object": {"sha": "commit0"}},
        status=200,
    )
    responses.add(responses.POST, f"{API}/repos/{REPO}/git/refs", json={}, status=201)
    responses.add(responses.DELETE, f"{API}/repos/{REPO}/git/refs/heads/work", body="", status=204)

    with pytest.raises(RuntimeError):
        with db.transaction(branch="work"):
            raise RuntimeError("boom")

    methods = [call.request.method for call in responses.calls]
    assert methods == ["GET", "POST", "DELETE"]
    assert not [call for call in responses.calls if call.request.method == "PATCH"]


@responses.activate
def test_transaction_keeps_the_work_branch_when_publishing_conflicts(db: GitDb) -> None:
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/git/ref/heads/main",
        json={"object": {"sha": "commit0"}},
        status=200,
    )
    responses.add(responses.POST, f"{API}/repos/{REPO}/git/refs", json={}, status=201)
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/git/ref/heads/work",
        json={"object": {"sha": "commit5"}},
        status=200,
    )
    responses.add(
        responses.PATCH,
        f"{API}/repos/{REPO}/git/refs/heads/main",
        json={"message": "Update is not a fast forward"},
        status=422,
    )
    with pytest.raises(ConflictError) as excinfo:
        with db.transaction(branch="work"):
            pass
    assert "work" in str(excinfo.value)
    assert not [call for call in responses.calls if call.request.method == "DELETE"]
