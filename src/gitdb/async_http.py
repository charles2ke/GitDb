"""The asyncio transport layer, mirroring :mod:`gitdb.http` on ``httpx``."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Dict, Optional

from .errors import AuthError, GitDbError, RateLimitError
from .http import (
    DEFAULT_API_URL,
    RETRY_STATUSES,
    backoff_seconds,
    default_headers,
    error_for,
    graphql_data,
    graphql_url_for,
    is_rate_limited,
    message_from,
)
from .ratelimit import RateLimit, RateLimiter

__all__ = ["AsyncGitHubClient", "require_httpx"]


def require_httpx() -> Any:
    """Import ``httpx`` with a helpful message when the extra is missing."""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise GitDbError(
            "the async client requires httpx; install it with: pip install 'gitdb-py[async]'"
        ) from exc
    return httpx


class AsyncGitHubClient:
    """The asyncio twin of :class:`~gitdb.http.GitHubClient`.

    Retries, rate-limit pacing and error translation are shared with the sync
    client; only the transport differs.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        api_url: str = DEFAULT_API_URL,
        client: Optional[Any] = None,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        max_backoff: float = 60.0,
        timeout: float = 30.0,
        auth: Optional[Any] = None,
        pace_requests: bool = True,
        graphql_url: Optional[str] = None,
    ) -> None:
        httpx = require_httpx()
        self.api_url = api_url.rstrip("/")
        self.graphql_url = graphql_url or graphql_url_for(self.api_url)
        self.token = token
        self.auth = auth
        self.max_retries = max(0, int(max_retries))
        self.backoff_factor = backoff_factor
        self.max_backoff = max_backoff
        self.timeout = timeout
        self.limiter = RateLimiter(enabled=pace_requests, max_delay=max_backoff)
        self._owns_client = client is None
        self.client: Any = client if client is not None else httpx.AsyncClient(timeout=timeout)
        if auth is not None:
            self.client.auth = auth

    # ------------------------------------------------------------------ utils
    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        return default_headers(self.token, extra)

    def _sleep_for(self, attempt: int, headers: Optional[Mapping[str, str]] = None) -> float:
        return backoff_seconds(
            attempt,
            headers,
            backoff_factor=self.backoff_factor,
            max_backoff=self.max_backoff,
        )

    @staticmethod
    def _message(response: Any) -> str:
        try:
            payload = response.json()
        except ValueError:
            return str(response.text or "")
        return message_from(payload, str(response.text or ""))

    # ------------------------------------------------------------- rate limit
    @property
    def rate_limit_state(self) -> Optional[RateLimit]:
        return self.limiter.state

    async def rate_limit(self, resource: str = "core") -> RateLimit:
        payload = await self.get_json("/rate_limit")
        return RateLimit.from_payload(payload, resource=resource)

    async def _pace(self) -> None:
        delay = self.limiter.delay()
        if delay > 0:
            await asyncio.sleep(delay)

    # ---------------------------------------------------------------- request
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json: Optional[Any] = None,
        headers: Optional[Mapping[str, str]] = None,
        allow_404: bool = False,
    ) -> Any:
        """Send a request and translate GitHub errors into GitDb exceptions."""
        httpx = require_httpx()
        url = path if path.startswith("http") else f"{self.api_url}/{path.lstrip('/')}"
        last_error: Optional[GitDbError] = None
        for attempt in range(self.max_retries + 1):
            await self._pace()
            try:
                response = await self.client.request(
                    method.upper(),
                    url,
                    params=dict(params) if params else None,
                    json=json,
                    headers=self._headers(headers),
                )
            except httpx.HTTPError as exc:  # pragma: no cover - network only
                last_error = GitDbError(f"request to {url} failed: {exc}")
                if attempt >= self.max_retries:
                    raise last_error from exc
                await asyncio.sleep(self._sleep_for(attempt))
                continue

            self.limiter.observe(response.headers)

            if response.status_code < 400:
                return response

            if is_rate_limited(response.status_code, response.headers, str(response.text or "")):
                if attempt >= self.max_retries:
                    raise self._rate_limit_error(response, attempt)
                await asyncio.sleep(self._sleep_for(attempt, response.headers))
                continue

            if response.status_code in RETRY_STATUSES:
                if attempt >= self.max_retries:
                    raise GitDbError(
                        f"GitHub API error {response.status_code}: {self._message(response)}",
                        status=response.status_code,
                    )
                await asyncio.sleep(self._sleep_for(attempt))
                continue

            if response.status_code == 404 and allow_404:
                return response

            raise error_for(response.status_code, self._message(response))
        raise last_error or GitDbError(f"request to {url} failed")  # pragma: no cover

    def _rate_limit_error(self, response: Any, attempt: int) -> RateLimitError:
        snapshot = RateLimit.from_headers(response.headers)
        return RateLimitError(
            f"GitHub API rate limit exceeded: {self._message(response)}",
            status=response.status_code,
            retry_after=self._sleep_for(attempt, response.headers),
            remaining=snapshot.remaining if snapshot else None,
            reset=snapshot.reset if snapshot else None,
        )

    # ------------------------------------------------------------- convenience
    async def get_json(self, path: str, **kwargs: Any) -> Any:
        response = await self.request("GET", path, **kwargs)
        return response.json()

    async def graphql(self, query: str, variables: Optional[Mapping[str, Any]] = None) -> Any:
        """Run a GraphQL query and return its ``data`` payload."""
        if not self.token and self.auth is None:
            raise AuthError("the GraphQL API requires authentication")
        response = await self.request(
            "POST",
            self.graphql_url,
            json={"query": query, "variables": dict(variables or {})},
        )
        return graphql_data(response.json())

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
