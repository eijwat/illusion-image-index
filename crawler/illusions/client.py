"""Polite HTTP client: one request at a time per host, minimum interval, honours 429/Retry-After."""
import logging
import time
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}
DEAD_HOST_FAILURES = 3


class PoliteClient:
    def __init__(self, user_agent: str, min_interval: dict[str, float], max_retries: int = 6):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.min_interval = min_interval          # host -> seconds
        self.max_retries = max_retries
        self._last: dict[str, float] = {}
        self._failures: dict[str, int] = {}      # consecutive network failures per host

    def _wait_turn(self, host: str) -> None:
        interval = self.min_interval.get(host, 1.0)
        elapsed = time.monotonic() - self._last.get(host, 0.0)
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last[host] = time.monotonic()

    def get(self, url: str, **kwargs) -> requests.Response:
        host = urlparse(url).hostname or ""
        if self._failures.get(host, 0) >= DEAD_HOST_FAILURES:
            # a host that keeps failing (gone, DNS dead) is skipped for the rest of this run
            raise requests.ConnectionError(f"host {host} unreachable ({DEAD_HOST_FAILURES} failures this run); skipped")
        backoff = 5.0
        for attempt in range(self.max_retries + 1):
            self._wait_turn(host)
            try:
                resp = self.session.get(url, timeout=(15, 120), **kwargs)
                self._failures[host] = 0
            except requests.RequestException as e:
                self._failures[host] = self._failures.get(host, 0) + 1
                if attempt == self.max_retries or self._failures[host] >= DEAD_HOST_FAILURES:
                    raise
                log.warning("network error on %s: %s; retry in %.0fs", host, e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 600)
                continue
            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                wait = _retry_after(resp) or backoff
                log.warning("HTTP %s from %s; waiting %.0fs", resp.status_code, host, wait)
                resp.close()
                time.sleep(wait)
                backoff = min(backoff * 2, 600)
                continue
            return resp
        raise RuntimeError("unreachable")


def _retry_after(resp: requests.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value and value.isdigit():
        return float(value)
    return None
