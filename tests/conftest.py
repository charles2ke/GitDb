"""Shared pytest fixtures for the GitDb test-suite."""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import quote

import pytest
import responses

from gitdb import GitDb

REPO = "owner/name"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never actually sleep during backoff tests."""
    monkeypatch.setattr("gitdb.http.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("gitdb.client.time.sleep", lambda _seconds: None)


@pytest.fixture
def db() -> GitDb:
    return GitDb(REPO, token="t0ken", branch="main", root="data", max_retries=2)


def encode(document: Mapping[str, Any]) -> str:
    payload = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def decode(content: str) -> Dict[str, Any]:
    data = json.loads(base64.b64decode(content).decode("utf-8"))
    assert isinstance(data, dict)
    return data


def contents_url(path: str) -> str:
    return f"{API}/repos/{REPO}/contents/{path}"


def contents_payload(path: str, document: Mapping[str, Any], sha: str = "sha1") -> Dict[str, Any]:
    return {
        "path": path,
        "sha": sha,
        "type": "file",
        "encoding": "base64",
        "content": encode(document),
    }


def tree_url(expression: str) -> str:
    """The Trees API url for a (possibly path-scoped) ref expression."""
    return f"{API}/repos/{REPO}/git/trees/{quote(expression, safe='')}"


def blob_url(sha: str) -> str:
    return f"{API}/repos/{REPO}/git/blobs/{sha}"


def tree_payload(entries: Iterable[Any], truncated: bool = False) -> Dict[str, Any]:
    """Build a Trees API payload from ``path`` or ``(path, sha)`` items."""
    tree: List[Dict[str, Any]] = []
    for entry in entries:
        path, sha = entry if isinstance(entry, tuple) else (entry, "b")
        tree.append({"path": path, "type": "blob", "sha": sha})
    return {"sha": "tree0", "truncated": truncated, "tree": tree}


def register_documents(
    documents: Mapping[str, Mapping[str, Any]], prefix: str = "data/users"
) -> None:
    """Register a scoped tree listing plus a raw blob response per document."""
    responses.add(
        responses.GET,
        tree_url(f"main:{prefix}"),
        json=tree_payload([(f"{doc_id}.json", f"blob-{doc_id}") for doc_id in documents]),
        status=200,
    )
    for doc_id, document in documents.items():
        responses.add(
            responses.GET,
            blob_url(f"blob-{doc_id}"),
            json=document,
            status=200,
        )


def register_commit_endpoints(
    blob_shas: Optional[List[str]] = None,
    *,
    base_commit: str = "commit0",
    commit_sha: str = "commit1",
    branch: str = "main",
) -> None:
    """Register the Git Data endpoints used by a batched commit."""
    for sha in blob_shas or []:
        responses.add(
            responses.POST, f"{API}/repos/{REPO}/git/blobs", json={"sha": sha}, status=201
        )
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/git/ref/heads/{branch}",
        json={"object": {"sha": base_commit}},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/git/commits/{base_commit}",
        json={"tree": {"sha": "tree0"}},
        status=200,
    )
    responses.add(
        responses.POST, f"{API}/repos/{REPO}/git/trees", json={"sha": "tree1"}, status=201
    )
    responses.add(
        responses.POST, f"{API}/repos/{REPO}/git/commits", json={"sha": commit_sha}, status=201
    )
    responses.add(
        responses.PATCH,
        f"{API}/repos/{REPO}/git/refs/heads/{branch}",
        json={"object": {"sha": commit_sha}},
        status=200,
    )


def bodies(method: str) -> List[Dict[str, Any]]:
    """Return the JSON bodies of every recorded request using ``method``."""
    return [
        json.loads(call.request.body)
        for call in responses.calls
        if call.request.method == method and call.request.body
    ]


def urls(method: Optional[str] = None) -> List[str]:
    return [
        call.request.url
        for call in responses.calls
        if method is None or call.request.method == method
    ]
