"""Email transport — TEST/ACTIVE dual flow selected by ACTIVE_ENV (backend.md § TEST/ACTIVE).

TEST (default): writes an .eml to a local, git-ignored folder — no real send, no keys needed.
ACTIVE: SendGrid. Both sit behind ONE interface and are resolved in ONE factory; callers depend on
`send_email`, never on a concrete adapter.
"""
import os
import uuid
from abc import ABC, abstractmethod
from email.message import EmailMessage

from src.config import settings


class EmailSender(ABC):
    @abstractmethod
    def send(self, to: str, subject: str, body: str) -> None: ...


class TestEmailSender(EmailSender):
    """Flow 1 (TEST): local file sink — same code path/shape as ACTIVE, no real send."""

    def send(self, to: str, subject: str, body: str) -> None:
        os.makedirs(settings.email_file_dir, exist_ok=True)
        msg = EmailMessage()
        msg["From"] = settings.email_from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        path = os.path.join(settings.email_file_dir, f"{uuid.uuid4()}.eml")
        with open(path, "wb") as fh:
            fh.write(bytes(msg))


class ActiveEmailSender(EmailSender):  # pragma: no cover - requires a live provider
    """Flow 2 (ACTIVE): SendGrid. Fails clearly if the key is missing — never degrades to TEST."""

    def send(self, to: str, subject: str, body: str) -> None:
        import httpx

        if not settings.sendgrid_api_key:
            raise RuntimeError("SENDGRID_API_KEY is required when ACTIVE_ENV=ACTIVE")
        resp = httpx.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {settings.sendgrid_api_key}"},
            json={
                "personalizations": [{"to": [{"email": to}]}],
                "from": {"email": settings.email_from},
                "subject": subject,
                "content": [{"type": "text/plain", "value": body}],
            },
            timeout=10.0,
        )
        resp.raise_for_status()


def get_email_sender() -> EmailSender:
    """The single factory — resolves the concrete adapter from ACTIVE_ENV (default TEST)."""
    if settings.active_env.upper() == "ACTIVE":
        return ActiveEmailSender()
    return TestEmailSender()


def send_email(to: str, subject: str, body: str) -> None:
    get_email_sender().send(to, subject, body)
