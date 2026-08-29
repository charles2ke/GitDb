"""Shared pytest fixtures for the GitDb test-suite."""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, Mapping

import pytest

from gitdb import GitDb

REPO = "owner/name"
API = "https://api.github.com"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never actually sleep during backoff tests."""
    monkeypatch.setattr("gitdb.http.time.sleep", lambda _seconds: None)


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
