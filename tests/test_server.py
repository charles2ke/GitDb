"""Tests for the GitDb Server web UI example; GitHub HTTP is mocked."""

from __future__ import annotations

from typing import Any, Dict, Iterator, List

import pytest
import responses
from fastapi.testclient import TestClient

from tests.conftest import API, REPO, blob_url, tree_payload, tree_url
from tests.test_examples import load_example

SERVER = load_example("server/main.py")
CREDENTIALS = {"repo": REPO, "token": "t0ken", "branch": "main", "root": "data"}


def register_root(entries: List[Dict[str, str]]) -> None:
    responses.add(
        responses.GET,
        f"{API}/repos/{REPO}/contents/data",
        json=entries,
        status=200,
    )


def register_users(documents: Dict[str, Dict[str, Any]]) -> None:
    responses.add(
        responses.GET,
        tree_url("main:data/users"),
        json=tree_payload([(f"{doc_id}.json", f"blob-{doc_id}") for doc_id in documents]),
        status=200,
    )
    for doc_id, document in documents.items():
        responses.add(responses.GET, blob_url(f"blob-{doc_id}"), json=document, status=200)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(SERVER.create_app()) as test_client:
        yield test_client


def sign_in(client: TestClient) -> Any:
    return client.post("/api/login", json=CREDENTIALS)


@responses.activate
def test_login_lists_collections_and_hides_derived_directories(client: TestClient) -> None:
    register_root(
        [
            {"name": "users", "type": "dir"},
            {"name": "notes", "type": "dir"},
            {"name": "_index", "type": "dir"},
            {"name": "README.md", "type": "file"},
        ]
    )
    response = sign_in(client)
    assert response.status_code == 200
    assert response.json()["collections"] == ["notes", "users"]
    assert client.cookies.get(SERVER.SESSION_COOKIE)


@responses.activate
def test_endpoints_require_a_session(client: TestClient) -> None:
    assert client.get("/api/collections").status_code == 401
    assert client.post("/api/query", json={"collection": "users"}).status_code == 401


@responses.activate
def test_query_returns_documents_and_columns(client: TestClient) -> None:
    register_root([{"name": "users", "type": "dir"}])
    sign_in(client)
    register_users({"a": {"_id": "a", "name": "Ada"}, "b": {"_id": "b", "name": "Grace"}})
    payload = client.post("/api/query", json={"collection": "users", "limit": 10}).json()
    assert payload["collection"] == "users"
    assert payload["columns"] == ["_id", "name"]
    assert [document["name"] for document in payload["documents"]] == ["Ada", "Grace"]


@responses.activate
def test_query_filters_by_field_value(client: TestClient) -> None:
    register_root([{"name": "users", "type": "dir"}])
    sign_in(client)
    register_users({"a": {"_id": "a", "name": "Ada"}, "b": {"_id": "b", "name": "Grace"}})
    payload = client.post(
        "/api/query", json={"collection": "users", "field": "name", "value": "Grace"}
    ).json()
    assert [document["_id"] for document in payload["documents"]] == ["b"]


@responses.activate
def test_query_rejects_invalid_collection_names(client: TestClient) -> None:
    register_root([{"name": "users", "type": "dir"}])
    sign_in(client)
    response = client.post("/api/query", json={"collection": "../etc"})
    assert response.status_code == 422


@responses.activate
def test_logout_drops_the_session(client: TestClient) -> None:
    register_root([{"name": "users", "type": "dir"}])
    sign_in(client)
    assert client.post("/api/logout").status_code == 204
    assert client.get("/api/collections").status_code == 401


def test_index_page_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "GitDb Server" in response.text
