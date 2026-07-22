"""Infra coverage: real file-email backend + the get_db dependency."""
from config.db_connection import get_db
from config.env_config import settings
from src.infrastructure.emailer import send_email


def test_file_email_backend_writes_eml(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "email_file_dir", str(tmp_path))
    send_email("a@example.com", "Hello", "Body text here")
    files = list(tmp_path.glob("*.eml"))
    assert len(files) == 1
    assert "Body text here" in files[0].read_text()


def test_get_db_yields_and_closes():
    gen = get_db()
    session = next(gen)
    assert session is not None
    gen.close()
