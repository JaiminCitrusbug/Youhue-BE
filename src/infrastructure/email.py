"""Minimal email sender (INFRA-01). File backend writes .eml to disk (dev); SendGrid in the
active env. INFRA-05 generalises this into the full transport with retry/backoff + delivery status.
"""
import os
import uuid
from email.message import EmailMessage

from src.config import settings


def send_email(to: str, subject: str, body: str) -> None:
    if settings.email_backend == "sendgrid":  # pragma: no cover - requires live provider
        _send_sendgrid(to, subject, body)
    else:
        _send_file(to, subject, body)


def _send_file(to: str, subject: str, body: str) -> None:
    os.makedirs(settings.email_file_dir, exist_ok=True)
    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    path = os.path.join(settings.email_file_dir, f"{uuid.uuid4()}.eml")
    with open(path, "wb") as fh:
        fh.write(bytes(msg))


def _send_sendgrid(to: str, subject: str, body: str) -> None:  # pragma: no cover
    import httpx

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
