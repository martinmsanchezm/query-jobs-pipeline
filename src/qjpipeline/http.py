"""A polite HTTP client for public job APIs (live mode only).

* Identifies itself with a User-Agent that links to this repository.
* Waits a minimum interval between requests to the same host.
* Retries 429 and 5xx responses and connection errors with backoff, honouring
  `Retry-After` up to a cap.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import __version__

USER_AGENT = f"query-jobs-pipeline/{__version__} (+https://github.com/nensanc/query-jobs-pipeline)"
RETRYABLE = frozenset({429, 500, 502, 503, 504})
MAX_WAIT_SECONDS = 30.0


class SourceError(Exception):
    """A source could not be read. The run logs it and continues with the others."""


class PoliteHttp:
    def __init__(
        self,
        *,
        min_interval: float = 1.0,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            transport=transport,
        )
        self._min_interval = min_interval
        self._retries = retries
        self._sleep = sleep
        self._last_call: dict[str, float] = {}
        self.requests = 0

    def close(self) -> None:
        self._client.close()

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        response = self._request(url, params)
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"{url} did not return JSON") from exc

    def _request(self, url: str, params: dict[str, Any] | None) -> httpx.Response:
        for attempt in range(self._retries + 1):
            self._respect_interval(urlsplit(url).netloc)
            self.requests += 1
            try:
                response = self._client.get(url, params=params)
            except httpx.TransportError as exc:
                if attempt == self._retries:
                    raise SourceError(f"could not reach {url}: {exc!r}") from exc
                self._sleep(2.0**attempt)
                continue
            if response.status_code in RETRYABLE and attempt < self._retries:
                self._sleep(_retry_delay(response, attempt))
                continue
            if response.status_code >= 400:
                raise SourceError(f"{url} answered HTTP {response.status_code}")
            return response
        raise AssertionError("unreachable")

    def _respect_interval(self, host: str) -> None:
        last = self._last_call.get(host)
        if last is not None:
            wait = self._min_interval - (time.monotonic() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_call[host] = time.monotonic()


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("Retry-After", "")
    if header.isdigit():
        return min(float(header), MAX_WAIT_SECONDS)
    return 2.0**attempt
