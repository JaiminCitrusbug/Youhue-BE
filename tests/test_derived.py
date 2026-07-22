"""INFRA-02 derived-value registry: single-owner contract + render-not-recompute (oracle)."""
import pytest

from src.application import derived


def test_single_owner_enforced():
    @derived.owns("test.value_x")
    def owner_a() -> int:
        return 1

    with pytest.raises(ValueError, match="already owned"):
        @derived.owns("test.value_x")
        def owner_b() -> int:
            return 2


def test_compute_dispatches_to_owner():
    @derived.owns("test.sum")
    def sum_owner(a: int, b: int) -> int:
        return a + b

    assert derived.compute("test.sum", 2, 3) == 5
    assert derived.owner_name("test.sum") == "sum_owner"


def test_unknown_value_raises():
    with pytest.raises(KeyError):
        derived.compute("test.nope")
    assert derived.owner_name("test.nope") is None


def test_render_not_recompute_oracle():
    @derived.owns("test.mean")
    def mean_owner(values: list[int]) -> float:
        return sum(values) / len(values)

    result = derived.compute("test.mean", [2, 4, 6])
    assert result == 4.0  # independent known-answer oracle, not a call to the owner


def test_phase1_values_are_registered_names():
    assert "flag.risk_score" in derived.REGISTERED_VALUES
    assert "class.mood_index" in derived.REGISTERED_VALUES
