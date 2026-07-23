"""In-process sliding-window rate limiter for the public / auth endpoints.

Postgres-only stack (no Redis, owner decision) -> this is per-process; acceptable for Phase-1
scale. (A shared limiter can be added with Redis if horizontal scale demands it.)

Counters are keyed on **(bucket, client)**, not on the client alone: each surface owns its budget,
so the unauthenticated school-registration endpoint (FR-02-01) can no longer exhaust the sign-in
budget that legitimate staff behind the same NAT egress depend on (review FR-02-01 Minor-4).

Client identity is the socket peer by default. Behind a reverse proxy the socket peer is the proxy,
so ``TRUST_PROXY_HEADERS=true`` switches to the left-most ``X-Forwarded-For`` entry. It is OFF by
default on purpose: trusting that header when no proxy rewrites it lets any caller forge a fresh
bucket per request and bypass the limiter entirely.
"""
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from config.env_config import settings

_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def reset() -> None:
    """Test hook — clear all counters."""
    _hits.clear()


def client_key(request: Request) -> str:
    """Who is being throttled. Only trusts ``X-Forwarded-For`` when the deployment says a proxy
    rewrites it; otherwise the header is attacker-controlled and would defeat the limiter."""
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _consume(bucket: str, request: Request, limit: int) -> None:
    key = (bucket, client_key(request))
    now = time.monotonic()
    window = _hits[key]
    cutoff = now - 60.0
    while window and window[0] <= cutoff:
        window.popleft()
    if len(window) >= limit:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests, slow down")
    window.append(now)


def rate_limit(request: Request) -> None:
    """Budget shared by the auth endpoints (sign-in / OTP-verify / forgot-password / SSO-start):
    blunts brute-force and enumeration probing against known accounts."""
    _consume("auth", request, settings.rate_limit_signin_per_minute)


def rate_limit_registration(request: Request) -> None:
    """Separate, tighter budget for public school self-registration (FR-02-01) — an anonymous,
    internet-facing write must not be able to throttle anyone's sign-in."""
    _consume("registration", request, settings.rate_limit_register_per_minute)
