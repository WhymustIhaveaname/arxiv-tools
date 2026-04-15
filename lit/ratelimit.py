"""Cross-process rate limiter + HTTP retry wrapper."""

from __future__ import annotations

import fcntl
import os
import sys
import time

import json5
import requests

from lit.config import CACHE_DIR

_RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)


def _brief_error(e: requests.RequestException) -> str:
    """Extract a short error string without leaking the full URL."""
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code}"
    return type(e).__name__


class RateLimiter:
    """Cross-process rate limiting via a json5 lock file.

    INTERVALS: minimum gap (seconds) between requests per service.
    RETRIES: max retry count; backoff = INTERVALS[service] * 2^attempt.
    """

    LOCK_FILE = CACHE_DIR / ".ratelimit.lock"
    RETRIES = 3
    INTERVALS = {
        "s2": 2.0,
        "arxiv": 5.0,
        "openalex": 0.1,
        # NCBI E-utilities: 3 req/s without an API key, 10 req/s with one.
        # 0.35s keeps us safely under the no-key limit; with a key we're still OK.
        "pubmed": 0.35,
        # Crossref polite pool (with mailto) is ~50 req/s; 0.05s is a safe cap.
        "crossref": 0.05,
        "ut": 0.3,
    }

    @classmethod
    def backoff(cls, service: str, attempt: int) -> float:
        return cls.INTERVALS[service] * (2 ** attempt)

    @classmethod
    def acquire(cls, service: str) -> None:
        """Atomically wait for the rate-limit window and record the request time.

        Holds an exclusive file lock during check+write so parallel processes
        cannot both pass the limiter simultaneously.
        """
        interval = cls.INTERVALS[service]
        for _ in range(5):
            with open(cls.LOCK_FILE, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.seek(0)
                content = f.read()
                try:
                    lock = json5.loads(content) if content.strip() else {}
                except ValueError:
                    lock = {}

                remaining = 0.0
                if service in lock:
                    remaining = interval - (time.time() - lock[service])

                if remaining <= 0:
                    lock[service] = time.time()
                    f.seek(0)
                    f.truncate()
                    f.write(json5.dumps(lock))
                    f.flush()
                    os.fsync(f.fileno())
                    return
            time.sleep(remaining)
        raise RuntimeError(
            f"RateLimiter: {service} failed to acquire request window after 5 attempts"
        )


def _request_with_retry(method, url, *, service: str, **kwargs) -> requests.Response:
    """HTTP request with rate-limiting and exponential backoff on 429/5xx."""
    last_err: requests.RequestException | None = None
    for attempt in range(RateLimiter.RETRIES + 1):
        RateLimiter.acquire(service)
        try:
            resp = method(url, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as e:
            retryable = (
                e.response is not None
                and e.response.status_code in _RETRYABLE_STATUS_CODES
            )
            if not retryable or attempt >= RateLimiter.RETRIES:
                raise
            msg = f"HTTP {e.response.status_code}"  # type: ignore[union-attr]
            last_err = e
        except requests.ConnectionError as e:
            if attempt >= RateLimiter.RETRIES:
                raise
            msg = "Connection error"
            last_err = e
        wait = RateLimiter.backoff(service, attempt)
        print(f"{msg}, {wait:.0f}s后重试...", file=sys.stderr)
        time.sleep(wait)
    raise last_err  # type: ignore[misc]
