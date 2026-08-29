from __future__ import annotations

import pytest
import responses

from gitdb import AuthError, GitDbError, GitHubClient, RateLimitError
from gitdb.http import DEFAULT_API_URL


@pytest.fixture
def client() -> GitHubClient:
    return GitHubClient("t0ken", max_retries=2, backoff_factor=0.01)


@responses.activate
def test_authorization_header_is_sent(client: GitHubClient) -> None:
    responses.add(responses.GET, f"{DEFAULT_API_URL}/rate_limit", json={"ok": True}, status=200)
    client.get_json("/rate_limit")
    header = responses.calls[0].request.headers["Authorization"]
    assert header == "Bearer " + client.token


@responses.activate
def test_unauthenticated_client_sends_no_authorization() -> None:
    responses.add(responses.GET, f"{DEFAULT_API_URL}/rate_limit", json={}, status=200)
    GitHubClient().get_json("/rate_limit")
    assert "Authorization" not in responses.calls[0].request.headers


@responses.activate
def test_rate_limit_is_retried_then_succeeds(client: GitHubClient) -> None:
    responses.add(
        responses.GET,
        f"{DEFAULT_API_URL}/rate_limit",
        json={"message": "API rate limit exceeded"},
        status=403,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1"},
    )
    responses.add(responses.GET, f"{DEFAULT_API_URL}/rate_limit", json={"ok": True}, status=200)

    assert client.get_json("/rate_limit") == {"ok": True}
    assert len(responses.calls) == 2


@responses.activate
def test_rate_limit_raises_after_max_retries(client: GitHubClient) -> None:
    for _ in range(4):
        responses.add(
            responses.GET,
            f"{DEFAULT_API_URL}/rate_limit",
            json={"message": "API rate limit exceeded"},
            status=429,
            headers={"Retry-After": "1"},
        )
    with pytest.raises(RateLimitError) as excinfo:
        client.get_json("/rate_limit")
    assert excinfo.value.status == 429
    assert len(responses.calls) == 3  # initial attempt + 2 retries


@responses.activate
def test_secondary_rate_limit_detected_from_body(client: GitHubClient) -> None:
    responses.add(
        responses.GET,
        f"{DEFAULT_API_URL}/x",
        json={"message": "You have exceeded a secondary rate limit"},
        status=403,
    )
    responses.add(responses.GET, f"{DEFAULT_API_URL}/x", json={"ok": True}, status=200)
    assert client.get_json("/x") == {"ok": True}


@responses.activate
def test_server_errors_are_retried(client: GitHubClient) -> None:
    responses.add(responses.GET, f"{DEFAULT_API_URL}/x", json={"message": "boom"}, status=502)
    responses.add(responses.GET, f"{DEFAULT_API_URL}/x", json={"ok": True}, status=200)
    assert client.get_json("/x") == {"ok": True}


@responses.activate
def test_server_errors_give_up(client: GitHubClient) -> None:
    for _ in range(4):
        responses.add(responses.GET, f"{DEFAULT_API_URL}/x", json={"message": "boom"}, status=503)
    with pytest.raises(GitDbError):
        client.get_json("/x")


@responses.activate
def test_bad_credentials_raise_auth_error(client: GitHubClient) -> None:
    responses.add(
        responses.GET, f"{DEFAULT_API_URL}/x", json={"message": "Bad credentials"}, status=401
    )
    with pytest.raises(AuthError):
        client.get_json("/x")


@responses.activate
def test_custom_session_and_auth_are_used() -> None:
    import requests

    session = requests.Session()
    client = GitHubClient(session=session, auth=("user", "pass"))
    assert client.session is session
    assert session.auth == ("user", "pass")


def test_backoff_uses_retry_after_header(client: GitHubClient) -> None:
    class FakeResponse:
        headers = {"Retry-After": "5"}

    delay = client._sleep_for(0, FakeResponse())  # type: ignore[arg-type]
    assert 5 <= delay <= 5 + client.backoff_factor


def test_backoff_is_exponential_with_jitter(client: GitHubClient) -> None:
    first = client._sleep_for(0, None)
    third = client._sleep_for(3, None)
    assert third > first
    assert third <= client.max_backoff
