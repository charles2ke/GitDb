"""Thin, retrying HTTP layer around the GitHub REST and GraphQL APIs."""

from __future__ import annotations

import random
import time
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

import requests

from .errors import (
    AuthError,
    ConflictError,
    GitDbError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)
from .ratelimit import RateLimit, RateLimiter

__all__ = ["GitHubClient", "DEFAULT_API_URL", "DEFAULT_RAW_URL", "graphql_url_for"]

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_RAW_URL = "https://raw.githubusercontent.com"
USER_AGENT = "gitdb-py"

#: Statuses that are safe to retry with exponential backoff.
_RETRY_STATUSES = frozenset({500, 502, 503, 504})

#: 422 messages that describe a lost race rather than a malformed request.
_CONFLICT_HINTS = ("sha", "exist", "conflict", "fast forward", "fast-forward")


def graphql_url_for(api_url: str) -> str:
    """Return the GraphQL endpoint matching a REST ``api_url``."""
    base = api_url.rstrip("/")
    if base.endswith("/api/v3"):
        return base[: -len("/v3")] + "/graphql"
    return base + "/graphql"


class GitHubClient:
    """Perform GitHub REST calls with rate-limit aware retries.

    Parameters
    ----------
    token:
        A raw personal access token (or app installation token). May be ``None``
        for unauthenticated/read-only usage.
    api_url:
        Base API url. Point this at ``https://github.example.com/api/v3`` for
        GitHub Enterprise Server.
    session:
        An injectable :class:`requests.Session`. Use it to supply custom auth,
        proxies or transport adapters.
    max_retries:
        Maximum number of retries for rate-limited and transient failures.
    pace_requests:
        Spread requests over the remaining quota once it runs low, instead of
        only reacting after GitHub returns 403/429.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        api_url: str = DEFAULT_API_URL,
        session: Optional[requests.Session] = None,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        max_backoff: float = 60.0,
        timeout: float = 30.0,
        auth: Optional[Any] = None,
        pace_requests: bool = True,
        graphql_url: Optional[str] = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.graphql_url = graphql_url or graphql_url_for(self.api_url)
        self.token = token
        self.session = session if session is not None else requests.Session()
        self.max_retries = max(0, int(max_retries))
        self.backoff_factor = backoff_factor
        self.max_backoff = max_backoff
        self.timeout = timeout
        self.limiter = RateLimiter(enabled=pace_requests, max_delay=max_backoff)
        if auth is not None:
            self.session.auth = auth

    # ------------------------------------------------------------------ utils
    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if extra:
            headers.update({key: value for key, value in extra.items() if value is not None})
        return headers

    def _sleep_for(self, attempt: int, response: Optional[requests.Response]) -> float:
        """Return the number of seconds to wait before the next attempt."""
        retry_after = None
        if response is not None:
            raw = response.headers.get("Retry-After")
            if raw:
                try:
                    retry_after = float(raw)
                except ValueError:
                    retry_after = None
            if retry_after is None:
                reset = response.headers.get("X-RateLimit-Reset")
                if reset and response.headers.get("X-RateLimit-Remaining") == "0":
                    try:
                        retry_after = max(0.0, float(reset) - time.time())
                    except ValueError:
                        retry_after = None
        if retry_after is None:
            retry_after = self.backoff_factor * (2**attempt)
        # Full jitter keeps concurrent clients from retrying in lockstep.
        return min(self.max_backoff, retry_after + random.random() * self.backoff_factor)

    @staticmethod
    def _is_rate_limited(response: requests.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        if response.headers.get("X-RateLimit-Remaining") == "0":
            return True
        if "Retry-After" in response.headers:
            return True
        return "rate limit" in (response.text or "").lower()

    @staticmethod
    def _message(response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.text or response.reason or ""
        if isinstance(payload, dict):
            message = payload.get("message")
            if isinstance(message, str):
                return message
        return str(payload)

    # ------------------------------------------------------------- rate limit
    @property
    def rate_limit_state(self) -> Optional[RateLimit]:
        """The most recent rate limit seen in a response header."""
        return self.limiter.state

    def rate_limit(self, resource: str = "core") -> RateLimit:
        """Query ``GET /rate_limit`` (a call that does not consume quota)."""
        payload = self.get_json("/rate_limit")
        return RateLimit.from_payload(payload, resource=resource)

    def _pace(self) -> None:
        delay = self.limiter.delay()
        if delay > 0:
            time.sleep(delay)

    # ---------------------------------------------------------------- request
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json: Optional[Any] = None,
        headers: Optional[Mapping[str, str]] = None,
        allow_404: bool = False,
    ) -> requests.Response:
        """Send a request and translate GitHub errors into GitDb exceptions.

        When ``allow_404`` is true a 404 response is returned to the caller
        instead of raising :class:`~gitdb.errors.NotFoundError`. ``304 Not
        Modified`` replies to conditional requests are returned as-is.
        """
        url = path if path.startswith("http") else f"{self.api_url}/{path.lstrip('/')}"
        last_error: Optional[GitDbError] = None
        for attempt in range(self.max_retries + 1):
            self._pace()
            try:
                response = self.session.request(
                    method.upper(),
                    url,
                    params=dict(params) if params else None,
                    json=json,
                    headers=self._headers(headers),
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:  # pragma: no cover - network only
                last_error = GitDbError(f"request to {url} failed: {exc}")
                if attempt >= self.max_retries:
                    raise last_error from exc
                time.sleep(self._sleep_for(attempt, None))
                continue

            self.limiter.observe(response.headers)

            if response.status_code < 400:
                return response

            if self._is_rate_limited(response):
                if attempt >= self.max_retries:
                    raise self._rate_limit_error(response, attempt)
                time.sleep(self._sleep_for(attempt, response))
                continue

            if response.status_code in _RETRY_STATUSES:
                if attempt >= self.max_retries:
                    raise GitDbError(
                        f"GitHub API error {response.status_code}: {self._message(response)}",
                        status=response.status_code,
                    )
                time.sleep(self._sleep_for(attempt, None))
                continue

            if response.status_code == 404 and allow_404:
                return response

            raise self._error_for(response)
        raise last_error or GitDbError(f"request to {url} failed")  # pragma: no cover

    def _rate_limit_error(self, response: requests.Response, attempt: int) -> RateLimitError:
        snapshot = RateLimit.from_headers(response.headers)
        return RateLimitError(
            f"GitHub API rate limit exceeded: {self._message(response)}",
            status=response.status_code,
            retry_after=self._sleep_for(attempt, response),
            remaining=snapshot.remaining if snapshot else None,
            reset=snapshot.reset if snapshot else None,
        )

    def _error_for(self, response: requests.Response) -> GitDbError:
        status = response.status_code
        message = self._message(response)
        if status in (401, 403):
            return AuthError(f"authentication failed ({status}): {message}", status=status)
        if status == 404:
            return NotFoundError(f"not found: {message}", status=status)
        if status == 409:
            return ConflictError(f"conflict: {message}", status=status)
        if status == 422:
            lowered = message.lower()
            if any(hint in lowered for hint in _CONFLICT_HINTS):
                return ConflictError(f"conflict: {message}", status=status)
            return ValidationError(f"invalid request: {message}", status=status)
        return GitDbError(f"GitHub API error {status}: {message}", status=status)

    # ------------------------------------------------------------- convenience
    def get_json(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs).json()

    def graphql(self, query: str, variables: Optional[Mapping[str, Any]] = None) -> Any:
        """Run a GraphQL query and return its ``data`` payload."""
        if not self.token and self.session.auth is None:
            raise AuthError("the GraphQL API requires authentication")
        response = self.request(
            "POST",
            self.graphql_url,
            json={"query": query, "variables": dict(variables or {})},
        )
        payload = response.json()
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            raise graphql_error(errors)
        return payload.get("data") if isinstance(payload, dict) else None

    def close(self) -> None:
        self.session.close()


def graphql_error(errors: List[Any]) -> GitDbError:
    """Translate a GraphQL ``errors`` array into a GitDb exception."""
    messages = []
    rate_limited = False
    for error in errors:
        if isinstance(error, Mapping):
            messages.append(str(error.get("message", error)))
            if error.get("type") == "RATE_LIMITED":
                rate_limited = True
        else:  # pragma: no cover - defensive
            messages.append(str(error))
    text = "; ".join(messages) or "unknown GraphQL error"
    if rate_limited:
        return RateLimitError(f"GraphQL rate limit exceeded: {text}")
    return GitDbError(f"GraphQL error: {text}")
