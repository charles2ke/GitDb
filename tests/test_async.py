"""The async client mirrored against the same scenarios as the sync suite."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional

import pytest

from gitdb import AuthError, ConflictError, NotFoundError, ValidationError
from gitdb.aio import AsyncGitDb
from tests.conftest import API, RAW, REPO, contents_payload, decode, encode, tree_url

httpx = pytest.importorskip("httpx")
respx = pytest.importorskip("respx")

ADA = "data/users/ada.json"
CONTENTS = f"{API}/repos/{REPO}/contents"
SCOPED = tree_url("main:data/users")


@pytest.fixture
def db() -> AsyncGitDb:
    return AsyncGitDb(REPO, token="t0ken", branch="main", root="data", max_retries=2)


def json_response(
    payload: Any, status: int = 200, headers: Optional[Mapping[str, str]] = None
) -> Any:
    return httpx.Response(status, json=payload, headers=dict(headers) if headers else None)


def register_documents(mock: Any, documents: Mapping[str, Mapping[str, Any]]) -> None:
    """Register a scoped tree listing plus a raw blob response per document."""
    mock.get(SCOPED).mock(
        return_value=json_response(
            {
                "sha": "tree0",
                "truncated": False,
                "tree": [
                    {"path": f"{doc_id}.json", "type": "blob", "sha": f"blob-{doc_id}"}
                    for doc_id in documents
                ],
            }
        )
    )
    for doc_id, document in documents.items():
        mock.get(f"{API}/repos/{REPO}/git/blobs/blob-{doc_id}").mock(
            return_value=json_response(document)
        )


def register_commit_endpoints(mock: Any, blob_shas: Optional[List[str]] = None) -> None:
    shas = list(blob_shas or [])
    mock.post(f"{API}/repos/{REPO}/git/blobs").mock(
        side_effect=[json_response({"sha": sha}, 201) for sha in shas] or None
    )
    mock.get(f"{API}/repos/{REPO}/git/ref/heads/main").mock(
        return_value=json_response({"object": {"sha": "commit0"}})
    )
    mock.get(f"{API}/repos/{REPO}/git/commits/commit0").mock(
        return_value=json_response({"tree": {"sha": "tree0"}})
    )
    mock.post(f"{API}/repos/{REPO}/git/trees").mock(
        return_value=json_response({"sha": "tree1"}, 201)
    )
    mock.post(f"{API}/repos/{REPO}/git/commits").mock(
        return_value=json_response({"sha": "commit1"}, 201)
    )
    mock.patch(f"{API}/repos/{REPO}/git/refs/heads/main").mock(
        return_value=json_response({"object": {"sha": "commit1"}})
    )


# ------------------------------------------------------------------ lifecycle
def test_repo_is_validated() -> None:
    with pytest.raises(ValidationError):
        AsyncGitDb("not-a-repo", token="t")


def test_credentials_are_required_for_writes() -> None:
    with pytest.raises(AuthError):
        AsyncGitDb(REPO)


def test_paths_match_the_sync_client() -> None:
    db = AsyncGitDb(REPO, token="t", shard_depth=1, shard_width=2)
    assert db.document_path("users", "abcd") == "data/users/ab/abcd.json"
    assert db.index_path("users", "email") == "data/_index/users/email.json"
    assert db.manifest_path("users") == "data/_manifest/users.json"
    assert AsyncGitDb.id_from_path("data/users/ab/abcd.json") == "abcd"


async def test_close_is_idempotent(db: AsyncGitDb) -> None:
    async with db:
        pass
    await db.aclose()


# ------------------------------------------------------------------------ CRUD
@respx.mock
async def test_insert_and_get(db: AsyncGitDb) -> None:
    put = respx.put(f"{CONTENTS}/{ADA}").mock(
        return_value=json_response({"content": {"sha": "sha1"}}, 201)
    )
    respx.get(f"{CONTENTS}/{ADA}").mock(
        return_value=json_response(contents_payload(ADA, {"_id": "ada", "name": "Ada"}))
    )
    assert await db.collection("users").insert({"name": "Ada"}, id="ada") == "ada"
    body = json.loads(put.calls[0].request.content)
    stored = decode(body["content"])
    assert stored["_id"] == "ada"
    assert stored["_rev"] == 1
    assert (await db.collection("users").get("ada"))["name"] == "Ada"


@respx.mock
async def test_get_returns_none_when_missing(db: AsyncGitDb) -> None:
    respx.get(f"{CONTENTS}/{ADA}").mock(return_value=json_response({"message": "Not Found"}, 404))
    assert await db.collection("users").get("ada") is None
    assert await db.collection("users").exists("ada") is False


@respx.mock
async def test_update_merges_and_bumps_the_revision(db: AsyncGitDb) -> None:
    respx.get(f"{CONTENTS}/{ADA}").mock(
        return_value=json_response(
            contents_payload(ADA, {"_id": "ada", "name": "Ada", "_rev": 2}, sha="sha1")
        )
    )
    put = respx.put(f"{CONTENTS}/{ADA}").mock(
        return_value=json_response({"content": {"sha": "sha2"}})
    )
    document = await db.collection("users").update("ada", {"city": "London"})
    assert document["name"] == "Ada"
    assert document["city"] == "London"
    assert document["_rev"] == 3
    assert document["_updated_at"]
    assert json.loads(put.calls[0].request.content)["sha"] == "sha1"


@respx.mock
async def test_update_of_a_missing_document_raises(db: AsyncGitDb) -> None:
    respx.get(f"{CONTENTS}/{ADA}").mock(return_value=json_response({"message": "Not Found"}, 404))
    with pytest.raises(NotFoundError):
        await db.collection("users").update("ada", {"x": 1})


@respx.mock
async def test_upsert_creates_when_missing(db: AsyncGitDb) -> None:
    respx.get(f"{CONTENTS}/{ADA}").mock(
        side_effect=[
            json_response({"message": "Not Found"}, 404),
            json_response(contents_payload(ADA, {"_id": "ada", "name": "Ada", "_rev": 1})),
        ]
    )
    respx.put(f"{CONTENTS}/{ADA}").mock(
        return_value=json_response({"content": {"sha": "sha1"}}, 201)
    )
    document = await db.collection("users").upsert("ada", {"name": "Ada"})
    assert document["name"] == "Ada"


@respx.mock
async def test_expected_rev_blocks_a_stale_write(db: AsyncGitDb) -> None:
    respx.get(f"{CONTENTS}/{ADA}").mock(
        return_value=json_response(contents_payload(ADA, {"_id": "ada", "_rev": 4}))
    )
    with pytest.raises(ConflictError):
        await db.collection("users").replace("ada", {"name": "Ada"}, expected_rev=2)


@respx.mock
async def test_delete_uses_the_known_sha(db: AsyncGitDb) -> None:
    respx.get(f"{CONTENTS}/{ADA}").mock(
        return_value=json_response(contents_payload(ADA, {"_id": "ada"}, sha="sha1"))
    )
    route = respx.delete(f"{CONTENTS}/{ADA}").mock(return_value=json_response({}))
    await db.collection("users").get("ada")
    await db.collection("users").delete("ada")
    assert json.loads(route.calls[0].request.content)["sha"] == "sha1"


@respx.mock
async def test_delete_of_a_missing_document_raises(db: AsyncGitDb) -> None:
    respx.get(f"{CONTENTS}/{ADA}").mock(return_value=json_response({"message": "Not Found"}, 404))
    with pytest.raises(NotFoundError):
        await db.collection("users").delete("ada")


@respx.mock
async def test_write_conflicts_are_retried(db: AsyncGitDb) -> None:
    respx.get(f"{CONTENTS}/{ADA}").mock(
        return_value=json_response(contents_payload(ADA, {"_id": "ada", "_rev": 1}, sha="sha1"))
    )
    respx.put(f"{CONTENTS}/{ADA}").mock(
        side_effect=[
            json_response({"message": "sha does not match"}, 409),
            json_response({"content": {"sha": "sha2"}}),
        ]
    )
    document = await db.collection("users").update("ada", {"name": "Ada"})
    assert document["_rev"] == 2


# --------------------------------------------------------------------- reading
@respx.mock
async def test_all_reads_documents_from_blobs_concurrently(db: AsyncGitDb) -> None:
    register_documents(
        respx, {"ada": {"_id": "ada", "name": "Ada"}, "bob": {"_id": "bob", "name": "Bob"}}
    )
    assert [doc["name"] for doc in await db.collection("users").all()] == ["Ada", "Bob"]
    blob_reads = [call for call in respx.calls if "/git/blobs/" in str(call.request.url)]
    assert len(blob_reads) == 2
    assert not [call for call in respx.calls if "/contents/" in str(call.request.url)]


@respx.mock
async def test_find_count_and_pages(db: AsyncGitDb) -> None:
    register_documents(respx, {letter: {"_id": letter} for letter in "abc"})
    users = db.collection("users")
    assert [doc["_id"] for doc in await users.find(lambda doc: doc["_id"] > "a")] == ["b", "c"]
    assert await users.count() == 3
    first = await users.page(2)
    assert [doc["_id"] for doc in first.documents] == ["a", "b"]
    assert first.cursor == "b"
    seen = [page async for page in users.pages(2)]
    assert [[doc["_id"] for doc in page] for page in seen] == [["a", "b"], ["c"]]


@respx.mock
async def test_async_iteration(db: AsyncGitDb) -> None:
    register_documents(respx, {"ada": {"_id": "ada"}})
    assert [doc["_id"] async for doc in db.collection("users")] == ["ada"]


@respx.mock
async def test_scoped_tree_falls_back_to_the_repository_tree(db: AsyncGitDb) -> None:
    respx.get(SCOPED).mock(return_value=json_response({"message": "Not Found"}, 404))
    respx.get(tree_url("main")).mock(
        return_value=json_response(
            {
                "sha": "t",
                "truncated": False,
                "tree": [
                    {"path": "data/users/ada.json", "type": "blob", "sha": "b1"},
                    {"path": "README.md", "type": "blob", "sha": "b2"},
                ],
            }
        )
    )
    assert await db.collection("users").list() == ["ada"]


@respx.mock
async def test_missing_collection_is_empty(db: AsyncGitDb) -> None:
    respx.get(SCOPED).mock(return_value=json_response({"message": "Not Found"}, 404))
    respx.get(tree_url("main")).mock(return_value=json_response({"message": "Not Found"}, 404))
    respx.get(f"{CONTENTS}/data/users").mock(
        return_value=json_response({"message": "Not Found"}, 404)
    )
    assert await db.collection("users").list() == []


@respx.mock
async def test_etag_revalidation_reuses_the_cached_body(db: AsyncGitDb) -> None:
    respx.get(f"{CONTENTS}/{ADA}").mock(
        side_effect=[
            json_response(contents_payload(ADA, {"_id": "ada"}), headers={"ETag": 'W/"1"'}),
            httpx.Response(304),
        ]
    )
    users = db.collection("users")
    assert await users.get("ada") == {"_id": "ada"}
    assert await users.get("ada", fresh=True) == {"_id": "ada"}


@respx.mock
async def test_large_documents_are_read_through_the_blob_api(db: AsyncGitDb) -> None:
    payload: Dict[str, Any] = contents_payload(ADA, {"_id": "ada"})
    payload.update({"content": "", "encoding": "none", "sha": "bigblob"})
    respx.get(f"{CONTENTS}/{ADA}").mock(return_value=json_response(payload))
    respx.get(f"{API}/repos/{REPO}/git/blobs/bigblob").mock(
        return_value=json_response({"_id": "ada", "name": "huge"})
    )
    assert (await db.collection("users").get("ada"))["name"] == "huge"


@respx.mock
async def test_blob_reads_tolerate_a_json_envelope(db: AsyncGitDb) -> None:
    payload: Dict[str, Any] = contents_payload(ADA, {"_id": "ada"})
    payload.update({"content": "", "encoding": "none", "sha": "bigblob"})
    respx.get(f"{CONTENTS}/{ADA}").mock(return_value=json_response(payload))
    respx.get(f"{API}/repos/{REPO}/git/blobs/bigblob").mock(
        return_value=json_response(
            {"sha": "bigblob", "encoding": "base64", "content": encode({"_id": "ada", "n": 1})}
        )
    )
    assert await db.collection("users").get("ada") == {"_id": "ada", "n": 1}


@respx.mock
async def test_large_documents_are_written_through_the_git_data_api() -> None:
    db = AsyncGitDb(REPO, token="t", contents_max_bytes=100)
    register_commit_endpoints(respx, ["big"])
    assert await db.collection("users").insert({"blob": "x" * 500}, id="big") == "big"
    assert not [call for call in respx.calls if "/contents/" in str(call.request.url)]


@respx.mock
async def test_read_only_pins_raw_urls_to_a_commit() -> None:
    db = AsyncGitDb(REPO, read_only=True)
    respx.get(f"{API}/repos/{REPO}/git/ref/heads/main").mock(
        return_value=json_response({"object": {"sha": "commit7"}})
    )
    respx.get(f"{RAW}/{REPO}/commit7/{ADA}").mock(return_value=json_response({"_id": "ada"}))
    assert await db.collection("users").get("ada") == {"_id": "ada"}
    with pytest.raises(AuthError):
        await db.collection("users").insert({"x": 1})


@respx.mock
async def test_at_and_snapshot_pin_reads(db: AsyncGitDb) -> None:
    respx.get(f"{API}/repos/{REPO}/git/ref/heads/main").mock(
        return_value=json_response({"object": {"sha": "commit9"}})
    )
    snapshot = await db.snapshot()
    assert snapshot.ref == "commit9"
    respx.get(f"{RAW}/{REPO}/commit1/{ADA}").mock(
        return_value=json_response({"_id": "ada", "name": "old"})
    )
    assert (await db.at("commit1").collection("users").get("ada"))["name"] == "old"


@respx.mock
async def test_graphql_bulk_fetch() -> None:
    db = AsyncGitDb(REPO, token="t", use_graphql=True)
    respx.get(SCOPED).mock(
        return_value=json_response(
            {
                "sha": "t",
                "truncated": False,
                "tree": [{"path": "ada.json", "type": "blob", "sha": "b1"}],
            }
        )
    )
    respx.post(f"{API}/graphql").mock(
        return_value=json_response(
            {"data": {"repository": {"d0": {"text": '{"_id": "ada"}', "oid": "b1"}}}}
        )
    )
    assert [doc["_id"] for doc in await db.collection("users").all()] == ["ada"]


# --------------------------------------------------------------------- batches
@respx.mock
async def test_batch_writes_one_commit(db: AsyncGitDb) -> None:
    register_commit_endpoints(respx, ["blobA", "blobB"])
    async with db.batch("seed") as batch:
        batch.put("users", "ada", {"name": "Ada"})
        batch.delete("users", "bob")
        assert batch.operations == 2

    tree = json.loads(
        next(
            call.request for call in respx.calls if str(call.request.url).endswith("/git/trees")
        ).content
    )
    assert tree["tree"] == [
        {"path": "data/users/ada.json", "mode": "100644", "type": "blob", "sha": "blobA"},
        {"path": "data/users/bob.json", "mode": "100644", "type": "blob", "sha": None},
    ]


@respx.mock
async def test_batch_is_discarded_on_error(db: AsyncGitDb) -> None:
    with pytest.raises(RuntimeError):
        async with db.batch("boom") as batch:
            batch.put("users", "ada", {"name": "Ada"})
            raise RuntimeError("boom")
    assert batch.operations == 0
    assert len(respx.calls) == 0


async def test_batch_put_increments_supplied_revision(db: AsyncGitDb) -> None:
    batch = db.batch("m")
    batch.put("users", "ada", {"_rev": 7, "name": "Ada"})
    assert batch._puts[ADA]["_rev"] == 8


@respx.mock
async def test_batch_preconditions_block_stale_writes(db: AsyncGitDb) -> None:
    respx.get(f"{CONTENTS}/{ADA}").mock(
        return_value=json_response(contents_payload(ADA, {"_id": "ada"}, sha="fresh"))
    )
    batch = db.batch("guarded")
    batch.put("users", "ada", {"name": "Ada"}, expected_sha="stale")
    assert batch.expectations == 1
    with pytest.raises(ConflictError):
        await batch.commit()
    assert not [call for call in respx.calls if str(call.request.url).endswith("/git/blobs")]


@respx.mock
async def test_batch_retries_when_the_branch_moved(db: AsyncGitDb) -> None:
    respx.post(f"{API}/repos/{REPO}/git/blobs").mock(
        return_value=json_response({"sha": "blobA"}, 201)
    )
    respx.get(f"{API}/repos/{REPO}/git/ref/heads/main").mock(
        side_effect=[
            json_response({"object": {"sha": "commit0"}}),
            json_response({"object": {"sha": "commit9"}}),
        ]
    )
    respx.get(f"{API}/repos/{REPO}/git/commits/commit0").mock(
        return_value=json_response({"tree": {"sha": "tree0"}})
    )
    respx.get(f"{API}/repos/{REPO}/git/commits/commit9").mock(
        return_value=json_response({"tree": {"sha": "tree9"}})
    )
    respx.post(f"{API}/repos/{REPO}/git/trees").mock(
        return_value=json_response({"sha": "tree1"}, 201)
    )
    respx.post(f"{API}/repos/{REPO}/git/commits").mock(
        side_effect=[
            json_response({"sha": "commit1"}, 201),
            json_response({"sha": "commit2"}, 201),
        ]
    )
    respx.patch(f"{API}/repos/{REPO}/git/refs/heads/main").mock(
        side_effect=[
            json_response({"message": "Update is not a fast forward"}, 422),
            json_response({"object": {"sha": "commit2"}}),
        ]
    )
    batch = db.batch("retry")
    batch.put("users", "ada", {"name": "Ada"})
    assert await batch.commit() == "commit2"
    blob_calls = [call for call in respx.calls if str(call.request.url).endswith("/git/blobs")]
    assert len(blob_calls) == 1


@respx.mock
async def test_indexed_writes_update_the_index_in_the_same_commit() -> None:
    db = AsyncGitDb(REPO, token="t", indexes={"users": ["email"]})
    respx.get(f"{CONTENTS}/data/_index/users/email.json").mock(
        return_value=json_response({"message": "Not Found"}, 404)
    )
    register_commit_endpoints(respx, ["blobIndex", "blobDoc"])
    async with db.batch("seed") as batch:
        batch.put("users", "ada", {"email": "ada@example.com"})

    written = [
        decode(json.loads(call.request.content)["content"])
        for call in respx.calls
        if str(call.request.url).endswith("/git/blobs") and call.request.method == "POST"
    ]
    index = next(body for body in written if body.get("_index") == "email")
    assert index["values"] == {"ada@example.com": ["ada"]}


@respx.mock
async def test_find_by_uses_the_index() -> None:
    db = AsyncGitDb(REPO, token="t", indexes={"users": ["email"]})
    index = {
        "_index": "email",
        "collection": "users",
        "values": {"ada@example.com": ["ada"]},
        "ids": {"ada": ["ada@example.com"]},
    }
    respx.get(f"{CONTENTS}/data/_index/users/email.json").mock(
        return_value=json_response(contents_payload("data/_index/users/email.json", index))
    )
    respx.get(f"{CONTENTS}/{ADA}").mock(
        return_value=json_response(
            contents_payload(ADA, {"_id": "ada", "email": "ada@example.com"})
        )
    )
    matches = await db.collection("users").find_by("email", "ada@example.com")
    assert [doc["_id"] for doc in matches] == ["ada"]


@respx.mock
async def test_reindex_rebuilds_the_index() -> None:
    db = AsyncGitDb(REPO, token="t", indexes={"users": ["email"]})
    register_documents(respx, {"ada": {"_id": "ada", "email": "a@x"}})
    register_commit_endpoints(respx, ["blobIndex"])
    assert await db.collection("users").reindex() == "commit1"


# ----------------------------------------------------------------- maintenance
@respx.mock
async def test_history(db: AsyncGitDb) -> None:
    respx.get(f"{API}/repos/{REPO}/commits").mock(
        return_value=json_response(
            [
                {
                    "sha": "c1",
                    "html_url": "https://github.com/owner/name/commit/c1",
                    "commit": {
                        "message": "insert users/ada",
                        "author": {"name": "Ada", "date": "2024-01-01T00:00:00Z"},
                    },
                }
            ]
        )
    )
    assert (await db.collection("users").history("ada"))[0]["sha"] == "c1"


@respx.mock
async def test_revert_commits_the_inverse(db: AsyncGitDb) -> None:
    respx.get(f"{API}/repos/{REPO}/commits/commit5").mock(
        return_value=json_response({"parents": [{"sha": "commit4"}], "files": [{"filename": ADA}]})
    )
    respx.get(f"{CONTENTS}/{ADA}").mock(
        return_value=json_response(contents_payload(ADA, {"_id": "ada", "name": "before"}))
    )
    register_commit_endpoints(respx, ["blobAda"])
    assert await db.revert("commit5") == "commit1"


@respx.mock
async def test_restore_writes_the_historical_document_forward(db: AsyncGitDb) -> None:
    respx.get(f"{CONTENTS}/{ADA}", params={"ref": "commit0"}).mock(
        return_value=json_response(contents_payload(ADA, {"_id": "ada", "name": "old", "_rev": 1}))
    )
    respx.get(f"{CONTENTS}/{ADA}", params={"ref": "main"}).mock(
        return_value=json_response(contents_payload(ADA, {"_id": "ada", "name": "new", "_rev": 9}))
    )
    respx.put(f"{CONTENTS}/{ADA}").mock(return_value=json_response({"content": {"sha": "sha2"}}))
    restored = await db.collection("users").restore("ada", "commit0")
    assert restored is not None
    assert (restored["name"], restored["_rev"]) == ("old", 10)


@respx.mock
async def test_compact_requires_confirmation(db: AsyncGitDb) -> None:
    with pytest.raises(ValidationError):
        await db.compact()


@respx.mock
async def test_compact_creates_a_parentless_commit(db: AsyncGitDb) -> None:
    respx.get(f"{API}/repos/{REPO}/git/ref/heads/main").mock(
        return_value=json_response({"object": {"sha": "commit9"}})
    )
    respx.get(f"{API}/repos/{REPO}/git/commits/commit9").mock(
        return_value=json_response({"tree": {"sha": "tree9"}})
    )
    commits = respx.post(f"{API}/repos/{REPO}/git/commits").mock(
        return_value=json_response({"sha": "squashed"}, 201)
    )
    respx.patch(f"{API}/repos/{REPO}/git/refs/heads/main").mock(
        return_value=json_response({"object": {"sha": "squashed"}})
    )
    assert await db.compact(confirm=True) == "squashed"
    assert json.loads(commits.calls[0].request.content)["parents"] == []


@respx.mock
async def test_rate_limit(db: AsyncGitDb) -> None:
    respx.get(f"{API}/rate_limit").mock(
        return_value=json_response(
            {"resources": {"core": {"limit": 5000, "remaining": 4321, "reset": 1}}}
        )
    )
    limit = await db.rate_limit()
    assert limit.remaining == 4321


@respx.mock
async def test_errors_are_translated(db: AsyncGitDb) -> None:
    respx.get(f"{CONTENTS}/{ADA}").mock(return_value=json_response({"message": "bad creds"}, 401))
    with pytest.raises(AuthError):
        await db.collection("users").get("ada")
