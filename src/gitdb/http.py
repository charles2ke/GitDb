"""Thin, retrying HTTP layer around the GitHub REST API."""

from __future__ import annotations

import random
import time
from collections.abc import Mapping
from typing import Any, Dict, Optional

import requests

from .errors import (
    AuthError,
    ConflictError,
    GitDbError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)

__all__ = ["GitHubClient", "DEFAULT_API_URL", "DEFAULT_RAW_URL"]

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_RAW_URL = "https://raw.githubusercontent.com"
USER_AGENT = "gitdb-py"

#: Statuses that are safe to retry with exponential backoff.
_RETRY_STATUSES = frozenset({500, 502, 503, 504})


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
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.session = session if session is not None else requests.Session()
        self.max_retries = max(0, int(max_retries))
        self.backoff_factor = backoff_factor
        self.max_backoff = max_backoff
        self.timeout = timeout
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
            headers.update(extra)
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
        instead of raising :class:`~gitdb.errors.NotFoundError`.
        """
        url = path if path.startswith("http") else f"{self.api_url}/{path.lstrip('/')}"
        last_error: Optional[GitDbError] = None
        for attempt in range(self.max_retries + 1):
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

            if response.status_code < 400:
                return response

            if self._is_rate_limited(response):
                if attempt >= self.max_retries:
                    raise RateLimitError(
                        f"GitHub API rate limit exceeded: {self._message(response)}",
                        status=response.status_code,
                        retry_after=self._sleep_for(attempt, response),
                    )
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
            if "sha" in lowered or "exist" in lowered or "conflict" in lowered:
                return ConflictError(f"conflict: {message}", status=status)
            return ValidationError(f"invalid request: {message}", status=status)
        return GitDbError(f"GitHub API error {status}: {message}", status=status)

    # ------------------------------------------------------------- convenience
    def get_json(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs).json()

    def close(self) -> None:
        self.session.close()
