"""In-process single-use token registry (jti consumed-once), TTL-pruned.

Postgres-only stack, no Redis (owner decision) -> this is per-process, the same tradeoff the
sliding-window rate limiter already makes (``middlewares.ratelimit``). It records the ``jti`` of a
signed, short-lived token the first time it is consumed so the token cannot be REPLAYED within its
TTL. Two callers use it: the SSO ``state`` token (m3 — one authorize round-trip per state) and the
SSO link-confirm token (M2 — one confirm per token). Acceptable for Phase-1 single-process scale; a
shared store (Redis / a DB table) can replace this module wholesale if horizontal scale demands it.
"""
import time

# jti -> monotonic expiry. A consumed jti is remembered until its expiry so a replay inside the
# token's own TTL always finds it; after expiry the token is invalid on its own (signature `exp`).
_consumed: dict[str, float] = {}


def reset() -> None:
    """Test hook — clear the registry (mirrors ``ratelimit.reset``)."""
    _consumed.clear()


def _prune(now: float) -> None:
    for jti, expiry in list(_consumed.items()):
        if expiry <= now:
            del _consumed[jti]


def consume(jti: str, ttl_seconds: float) -> bool:
    """Record ``jti`` as used. Return True on the FIRST use (accept), False if already consumed
    (replay). ``ttl_seconds`` should be >= the token's remaining lifetime so the marker outlives the
    token it guards."""
    now = time.monotonic()
    _prune(now)
    if jti in _consumed:
        return False
    _consumed[jti] = now + ttl_seconds
    return True
