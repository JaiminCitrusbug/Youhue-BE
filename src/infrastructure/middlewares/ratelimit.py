"""In-process sliding-window rate limiter for auth endpoints.

Postgres-only stack (no Redis, owner decision) -> this is per-process; acceptable for Phase-1
scale. It throttles sign-in / OTP-verify / forgot-password by client IP to blunt brute-force and
enumeration probing. (A shared limiter can be added with Redis if horizontal scale demands it.)
"""
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from config.env_config import settings

_hits: dict[str, deque[float]] = defaultdict(deque)


def reset() -> None:
    """Test hook — clear all counters."""
    _hits.clear()


def rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    window = _hits[key]
    cutoff = now - 60.0
    while window and window[0] <= cutoff:
        window.popleft()
    if len(window) >= settings.rate_limit_signin_per_minute:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests, slow down")
    window.append(now)
