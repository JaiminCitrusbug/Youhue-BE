"""Health endpoint test — proves the app boots and serves GET /health."""
from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
