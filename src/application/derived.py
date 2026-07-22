"""Derived-value single-owner registry (§13.5 / BR-05: render, never recompute).

Each derived value is computed by exactly ONE owner. Feature modules register their owner with
`@owns(name)`; consumers call `compute(name, ...)` — they never reimplement the formula. Registering
a second owner for the same value is a hard error, which enforces the single-owner contract.
Owners themselves land in their tickets (dashboard mood_index/participation FR-10-*, risk
score/band INFRA-06/FR-12-06, subscription.state FR-17-*, platform.stats FR-19-07).
"""
from collections.abc import Callable
from typing import Any

_OWNERS: dict[str, Callable[..., Any]] = {}

# Phase-1 derived values that MUST have exactly one owner (§13.5 register).
REGISTERED_VALUES = (
    "class.mood_index",
    "class.trend",
    "student.participation_rate",
    "flag.risk_score",
    "flag.band",
    "subscription.state",
    "platform.stats",
)


def owns(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        existing = _OWNERS.get(name)
        if existing is not None:
            raise ValueError(f"Derived value '{name}' already owned by {existing.__name__}")
        _OWNERS[name] = fn
        return fn

    return decorator


def compute(name: str, *args: Any, **kwargs: Any) -> Any:
    owner = _OWNERS.get(name)
    if owner is None:
        raise KeyError(f"No owner registered for derived value '{name}'")
    return owner(*args, **kwargs)


def owner_name(name: str) -> str | None:
    owner = _OWNERS.get(name)
    return owner.__name__ if owner is not None else None
