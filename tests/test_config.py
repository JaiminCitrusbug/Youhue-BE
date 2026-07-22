"""Settings load + env-contract sanity (adapters/thresholds are env-driven)."""
from src.config import Settings


def test_settings_load_with_defaults() -> None:
    s = Settings()
    assert s.api_prefix == "/api/v1"
    assert s.active_env in {"TEST", "ACTIVE"}
    assert 0.0 <= s.risk_triage_threshold <= s.risk_immediate_threshold <= 1.0


def test_mfa_roles_parsed() -> None:
    s = Settings(mfa_required_roles="leadership, admin ,")
    assert s.mfa_roles == ["leadership", "admin"]


def test_concern_words_normalised() -> None:
    s = Settings(default_concern_words="Hurt, ALONE ,")
    assert s.concern_words == ["hurt", "alone"]
