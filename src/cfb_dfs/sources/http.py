"""Shared HTTP helpers: one client factory and one retry policy."""

from __future__ import annotations

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from cfb_dfs.logging_setup import get_logger

log = get_logger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 cfb-dfs-data/0.1"
)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class RetryableHTTPError(Exception):
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"HTTP {response.status_code} for {response.request.url}")


class SourceError(Exception):
    """A source failed in a way that should mark its tab stale but not stop the run."""


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, RetryableHTTPError | httpx.TransportError)


def make_client(base_url: str = "", headers: dict[str, str] | None = None) -> httpx.Client:
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        h.update(headers)
    return httpx.Client(
        base_url=base_url, headers=h, timeout=httpx.Timeout(60.0), follow_redirects=True
    )


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=30),
    reraise=True,
)
def get_with_retry(client: httpx.Client, url: str, params: dict | None = None) -> httpx.Response:
    resp = client.get(url, params=params)
    if resp.status_code in RETRYABLE_STATUS:
        log.warning("http.retryable", url=str(resp.request.url), status=resp.status_code)
        raise RetryableHTTPError(resp)
    resp.raise_for_status()
    return resp
