from __future__ import annotations

import pytest

from gitdb import AuthError, GitDb, ValidationError, new_id, new_uuid
from gitdb.ids import validate_name


def test_new_id_is_sortable_and_unique() -> None:
    ids = [new_id() for _ in range(50)]
    assert len(set(ids)) == 50
    assert ids == sorted(ids) or sorted(ids)[0][:10] == ids[0][:10]
    assert all(len(value) == 26 for value in ids)


def test_new_uuid() -> None:
    assert len(new_uuid()) == 36


@pytest.mark.parametrize(
    "bad",
    [
        "",
        ".",
        "..",
        "../etc/passwd",
        "a/b",
        "a\\b",
        "-leading",
        "with space",
        "x" * 129,
        "sneaky..id",
    ],
)
def test_validate_id_rejects_unsafe_values(bad: str) -> None:
    with pytest.raises(ValidationError):
        GitDb("owner/name", token="t").document_path("users", bad)


@pytest.mark.parametrize("good", ["abc", "A1", "01HZY.z_-", "u" * 128])
def test_validate_id_accepts_safe_values(good: str) -> None:
    db = GitDb("owner/name", token="t")
    assert db.document_path("users", good) == f"data/users/{good}.json"


def test_validate_id_requires_string() -> None:
    with pytest.raises(ValidationError):
        GitDb("owner/name", token="t").document_path("users", 5)  # type: ignore[arg-type]


def test_validate_name_rejects_traversal() -> None:
    with pytest.raises(ValidationError):
        validate_name("../secrets")


def test_repo_must_be_owner_slash_name() -> None:
    with pytest.raises(ValidationError):
        GitDb("not-a-repo", token="t")


def test_token_required_unless_read_only() -> None:
    with pytest.raises(AuthError):
        GitDb("owner/name")
    assert GitDb("owner/name", read_only=True).read_only


def test_sharded_paths() -> None:
    db = GitDb("owner/name", token="t", shard_depth=2, shard_width=2)
    assert db.document_path("users", "01HZAB") == "data/users/01/HZ/01HZAB.json"


def test_root_can_be_repository_root() -> None:
    db = GitDb("owner/name", token="t", root="")
    assert db.document_path("users", "a1") == "users/a1.json"


def test_id_from_path() -> None:
    assert GitDb.id_from_path("data/users/a1.json") == "a1"
    assert GitDb.id_from_path("data/users/readme.md") is None
