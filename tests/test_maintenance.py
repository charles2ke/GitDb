from __future__ import annotations

import json
import time
from typing import Any, Dict, List

import pytest
import responses

from gitdb import (
    ConflictError,
    GitDb,
    InstallationTokenAuth,
    RateLimit,
    RateLimitError,
    ValidationError,
)
from gitdb.auth import _as_epoch
from gitdb.ratelimit import RateLimiter
from tests.conftest import API, REPO, contents_payload, contents_url, register_commit_endpoints

ADA = "data/users/ada.json"


def existing(document: Dict[str, Any], sha: str = "sha1") -> None:
    responses.add(
        responses.GET, contents_url(ADA), json=contents_payload(ADA, document, sha), status=200
    )


# --------------------------------------------------------------- rate limiting
def test_rate_limit_reads_headers() -> None:
    reset = int(time.time()) + 60
    limit = RateLimit.from_headers(
        {
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "10",
            "X-RateLimit-Reset": str(reset),
        }
    )
    assert limit is not None
    assert (limit.limit, limit.remaining) == (5000, 10)
    assert limit.exhausted is False
    assert 0 < limit.seconds_until_reset <= 60
    assert limit.reset_at is not None


def test_rate_limit_headers_are_optional() -> None:
    assert RateLimit.from_headers({}) is None


def test_limiter_paces_when_the_quota_is_nearly_gone() -> None:
    limiter = RateLimiter(enabled=True)
    limiter.observe(
        {
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "1",
            "X-RateLimit-Reset": str(int(time.time()) + 100),
        }
    )
    assert limiter.delay() > 0


def test_limiter_does_not_pace_with_plenty_of_quota() -> None:
    limiter = RateLimiter(enabled=True)
    limiter.observe(
        {
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "4999",
            "X-RateLimit-Reset": str(int(time.time()) + 3600),
        }
    )
    assert limiter.delay() == 0


def test_disabled_limiter_never_delays() -> None:
    limiter = RateLimiter(enabled=False)
    limiter.observe({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 60)})
    assert limiter.delay() == 0


@responses.activate
def test_db_rate_limit_queries_the_endpoint(db: GitDb) -> None:
    reset = int(time.time()) + 60
    responses.add(
        responses.GET,
        f"{API}/rate_limit",
        json={"resources": {"core": {"limit": 5000, "remaining": 4000, "reset": reset}}},
        status=200,
    )
    limit = db.rate_limit()
    assert (limit.limit, limit.remaining) == (5000, 4000)


@responses.activate
def test_rate_limit_error_exposes_remaining_and_reset() -> None:
    db = GitDb(REPO, token="t", max_retries=0)
    reset = int(time.time()) + 30
    responses.add(
        responses.GET,
        contents_url(ADA),
        json={"message": "API rate limit exceeded"},
        status=403,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)},
    )
    with pytest.raises(RateLimitError) as excinfo:
        db.collection("users").get("ada")
    assert excinfo.value.remaining == 0
    assert excinfo.value.reset == reset
    assert excinfo.value.reset_at is not None


@responses.activate
def test_the_client_paces_itself_between_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: List[float] = []
    monkeypatch.setattr("gitdb.http.time.sleep", lambda seconds: slept.append(seconds))
    db = GitDb(REPO, token="t", pace_requests=True)
    headers = {
        "X-RateLimit-Limit": "5000",
        "X-RateLimit-Remaining": "1",
        "X-RateLimit-Reset": str(int(time.time()) + 120),
    }
    responses.add(
        responses.GET,
        contents_url(ADA),
        json=contents_payload(ADA, {"_id": "ada"}),
        status=200,
        headers=headers,
    )
    responses.add(
        responses.GET,
        contents_url(ADA),
        json=contents_payload(ADA, {"_id": "ada"}, sha="sha2"),
        status=200,
        headers=headers,
    )
    db.collection("users").get("ada")
    db.collection("users").get("ada", fresh=True)
    assert slept and slept[0] > 0


@responses.activate
def test_pacing_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: List[float] = []
    monkeypatch.setattr("gitdb.http.time.sleep", lambda seconds: slept.append(seconds))
    db = GitDb(REPO, token="t", pace_requests=False)
    headers = {"X-RateLimit-Remaining": "1", "X-RateLimit-Reset": str(int(time.time()) + 120)}
    for sha in ("sha1", "sha2"):
        responses.add(
            responses.GET,
            contents_url(ADA),
            json=contents_payload(ADA, {"_id": "ada"}, sha=sha),
            status=200,
            headers=headers,
        )
    db.collection("users").get("ada")
    db.collection("users").get("ada", fresh=True)
    assert slept == []


# ------------------------------------------------------------------------ auth
def test_installation_token_auth_refreshes_expired_tokens() -> None:
    minted: List[str] = []

    def factory() -> Any:
        minted.append("call")
        return f"token{len(minted)}", time.time() + 3600

    auth = InstallationTokenAuth(factory)
    assert auth.token() == "token1"
    assert auth.token() == "token1"
    auth._expires_at = time.time() - 1
    assert auth.token() == "token2"


def test_installation_token_auth_sets_the_header() -> None:
    auth = InstallationTokenAuth(lambda: "abc")

    class Request:
        headers: Dict[str, str] = {}

    request = Request()
    auth(request)
    header = request.headers["Authorization"]
    assert header.startswith("Bearer ")
    assert header.split(" ", 1)[1] == "abc"


def test_installation_token_auth_validates_its_input() -> None:
    from gitdb import AuthError

    with pytest.raises(AuthError):
        InstallationTokenAuth("not callable")  # type: ignore[arg-type]
    with pytest.raises(AuthError):
        InstallationTokenAuth(lambda: "").token()


def test_expiry_parsing_accepts_several_formats() -> None:
    assert _as_epoch(None) is None
    assert _as_epoch(123.0) == 123.0
    assert _as_epoch("2024-01-01T00:00:00Z") == 1704067200.0
    assert _as_epoch("nonsense") is None


@responses.activate
def test_auth_object_is_used_for_requests() -> None:
    db = GitDb(REPO, auth=InstallationTokenAuth(lambda: "installation"))
    responses.add(
        responses.GET, contents_url(ADA), json=contents_payload(ADA, {"_id": "ada"}), status=200
    )
    db.collection("users").get("ada")
    header = responses.calls[0].request.headers["Authorization"]
    assert header.split(" ", 1) == ["Bearer", "installation"]


# ------------------------------------------------------- compare-and-set writes
@responses.activate
def test_expected_rev_blocks_a_stale_replace(db: GitDb) -> None:
    existing({"_id": "ada", "_rev": 4})
    with pytest.raises(ConflictError):
        db.collection("users").replace("ada", {"name": "Ada"}, expected_rev=2)
    assert not [call for call in responses.calls if call.request.method == "PUT"]


@responses.activate
def test_expected_rev_allows_the_matching_revision(db: GitDb) -> None:
    existing({"_id": "ada", "_rev": 4})
    responses.add(responses.PUT, contents_url(ADA), json={"content": {"sha": "sha2"}}, status=200)
    document = db.collection("users").replace("ada", {"name": "Ada"}, expected_rev=4)
    assert document["_rev"] == 5


@responses.activate
def test_expected_rev_blocks_a_stale_update(db: GitDb) -> None:
    existing({"_id": "ada", "_rev": 4})
    with pytest.raises(ConflictError):
        db.collection("users").update("ada", {"name": "Ada"}, expected_rev=1)


@responses.activate
def test_expected_rev_blocks_a_stale_delete(db: GitDb) -> None:
    existing({"_id": "ada", "_rev": 4})
    with pytest.raises(ConflictError):
        db.collection("users").delete("ada", expected_rev=1)
    assert not [call for call in responses.calls if call.request.method == "DELETE"]


# --------------------------------------------------------------- undo & history
@responses.activate
def test_restore_writes_the_historical_document_forward(db: GitDb) -> None:
    responses.add(
        responses.GET,
        contents_url(ADA),
        json=contents_payload(ADA, {"_id": "ada", "name": "old", "_rev": 1}),
        status=200,
        match=[responses.matchers.query_param_matcher({"ref": "commit0"})],
    )
    responses.add(
        responses.GET,
        contents_url(ADA),
        json=contents_payload(ADA, {"_id": "ada", "name": "new", "_rev": 9}),
        status=200,
        match=[responses.matchers.query_param_matcher({"ref": "main"})],
    )
    responses.add(responses.PUT, contents_url(ADA), json={"content": {"sha": "sha2"}}, status=200)
    restored = db.collection("users").restore("ada", "commit0")
    assert restored is not None
    assert restored["name"] == "old"
    assert restored["_rev"] == 10


@responses.activate
def test_restore_deletes_documents_that_did_not_exist_yet(db: GitDb) -> None:
    responses.add(
        responses.GET,
        contents_url(ADA),
        json={"message": "Not Found"},
        status=404,
        match=[responses.matchers.query_param_matcher({"ref": "commit0"})],
    )
    responses.add(
        responses.GET,
        contents_url(ADA),
        json=contents_payload(ADA, {"_id": "ada"}),
        status=200,
        match=[responses.matchers.query_param_matcher({"ref": "main"})],
    )
    responses.add(responses.DELETE, contents_url(ADA), json={}, status=200)
    assert db.collection("users").restore("ada", "commit0") is None
    assert [call.request.method for call in responses.calls][-1] == "DELETE"


@responses.activate
def test_revert_commits_the_inverse_of_a_commit(db: GitDb) -> None:
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/commits/commit5",
        json={
            "sha": "commit5",
            "parents": [{"sha": "commit4"}],
            "files": [
                {"filename": ADA},
                {"filename": "data/users/bob.json"},
                {"filename": "README.md"},
            ],
        },
        status=200,
    )
    responses.add(
        responses.GET,
        contents_url(ADA),
        json=contents_payload(ADA, {"_id": "ada", "name": "before"}),
        status=200,
    )
    responses.add(
        responses.GET,
        contents_url("data/users/bob.json"),
        json={"message": "Not Found"},
        status=404,
    )
    register_commit_endpoints(["blobAda"])
    assert db.revert("commit5") == "commit1"

    tree = next(
        json.loads(call.request.body)
        for call in responses.calls
        if call.request.url.endswith("/git/trees")
    )
    entries = {entry["path"]: entry["sha"] for entry in tree["tree"]}
    assert entries == {ADA: "blobAda", "data/users/bob.json": None}


@responses.activate
def test_revert_of_a_root_commit_is_rejected(db: GitDb) -> None:
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/commits/commit0",
        json={"sha": "commit0", "parents": [], "files": []},
        status=200,
    )
    with pytest.raises(ValidationError):
        db.revert("commit0")


@responses.activate
def test_revert_without_data_changes_does_nothing(db: GitDb) -> None:
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/commits/commit5",
        json={"parents": [{"sha": "c4"}], "files": [{"filename": "README.md"}]},
        status=200,
    )
    assert db.revert("commit5") is None


# ------------------------------------------------------------------ compaction
@responses.activate
def test_compact_requires_confirmation(db: GitDb) -> None:
    with pytest.raises(ValidationError):
        db.compact()
    assert len(responses.calls) == 0


@responses.activate
def test_compact_creates_a_parentless_commit(db: GitDb) -> None:
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
        responses.POST, f"{API}/repos/{REPO}/git/commits", json={"sha": "squashed"}, status=201
    )
    responses.add(
        responses.PATCH,
        f"{API}/repos/{REPO}/git/refs/heads/main",
        json={"object": {"sha": "squashed"}},
        status=200,
    )
    assert db.compact(confirm=True) == "squashed"
    commit = json.loads(responses.calls[2].request.body)
    assert commit["parents"] == []
    assert commit["tree"] == "tree9"
    assert json.loads(responses.calls[3].request.body) == {"sha": "squashed", "force": True}
