"""INFRA-05 notification transport: dual-channel, retry+backoff+surface (no silent loss), feed,
idempotency, worker due-selection, channel-appropriate content."""
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

import src.application.notifications.services as notif_mod
from src.application.notifications import services as notif
from src.constants.enums import AlertChannel, DeliveryStatus
from src.domain.billing.models import Notification
from src.domain.risk.models import AlertDelivery

SIGNIN = "/api/v1/auth/staff/sign-in"


def _now() -> datetime:
    return datetime.now(UTC)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(client, email: str) -> str:
    return client.post(SIGNIN, json={"email": email, "password": "Password123"}).json()["access_token"]


def _boom(to: str, subject: str, body: str) -> None:
    raise RuntimeError("smtp down")


def _deliveries(db, notification_id: uuid.UUID) -> dict[AlertChannel, AlertDelivery]:
    rows = db.scalars(
        select(AlertDelivery).where(AlertDelivery.notification_id == notification_id)
    ).all()
    return {r.channel: r for r in rows}


def test_enqueue_creates_message_and_channel_deliveries(db, make_school, make_staff):
    recipient = make_staff(make_school(), email="r@oakwood.edu")
    n = notif.enqueue(db, recipient_id=recipient.id, ntype="alert", payload={"reason": "concern word"})
    db.commit()
    d = _deliveries(db, n.id)
    assert set(d) == {AlertChannel.in_app, AlertChannel.email}
    assert d[AlertChannel.in_app].status == DeliveryStatus.delivered  # redundancy, stored inline
    assert d[AlertChannel.email].status == DeliveryStatus.queued
    assert d[AlertChannel.email].next_attempt_at is not None  # queued for the worker


def test_worker_sends_due_email(db, make_school, make_staff):
    recipient = make_staff(make_school(), email="r@oakwood.edu")
    n = notif.enqueue(db, recipient_id=recipient.id, ntype="alert", payload={"reason": "x"})
    db.commit()
    processed = notif.process_due_deliveries(db)
    db.commit()
    assert processed == 1
    email = _deliveries(db, n.id)[AlertChannel.email]
    assert email.status == DeliveryStatus.sent  # 'delivered' awaits the provider webhook
    assert email.next_attempt_at is None


def test_backoff_grows_with_attempts(db):
    assert notif.backoff_delay(1).total_seconds() == notif.BASE_BACKOFF_SECONDS
    assert notif.backoff_delay(1) < notif.backoff_delay(2) < notif.backoff_delay(3)


def test_due_selection_skips_not_yet_due(db, make_school, make_staff):
    recipient = make_staff(make_school(), email="r@oakwood.edu")
    n = notif.enqueue(db, recipient_id=recipient.id, ntype="t", payload=None)
    db.commit()
    email = _deliveries(db, n.id)[AlertChannel.email]
    email.next_attempt_at = _now() + timedelta(hours=1)  # scheduled for later
    db.commit()
    assert notif.process_due_deliveries(db) == 0  # not due -> not picked up
    db.commit()
    db.refresh(email)
    assert email.status == DeliveryStatus.queued  # untouched


def test_retrying_sets_backoff_before_exhaustion(db, make_school, make_staff, monkeypatch):
    monkeypatch.setattr(notif_mod, "send_email", _boom)
    recipient = make_staff(make_school(), email="r@oakwood.edu")
    n = notif.enqueue(db, recipient_id=recipient.id, ntype="t", payload=None)
    db.commit()
    notif.process_due_deliveries(db)
    db.commit()
    email = _deliveries(db, n.id)[AlertChannel.email]
    assert email.status == DeliveryStatus.retrying
    assert email.attempts == 1
    assert email.next_attempt_at > _now()  # backoff scheduled into the future


def test_failed_send_retries_then_surfaces_not_lost(db, make_school, make_staff, monkeypatch):
    monkeypatch.setattr(notif_mod, "send_email", _boom)
    recipient = make_staff(make_school(), email="r@oakwood.edu")
    n = notif.enqueue(db, recipient_id=recipient.id, ntype="alert", payload={"x": 1})
    db.commit()
    email_id = _deliveries(db, n.id)[AlertChannel.email].id
    for _ in range(notif.MAX_ATTEMPTS):
        d = db.get(AlertDelivery, email_id)
        d.next_attempt_at = _now()  # simulate each backoff window elapsing
        db.commit()
        notif.process_due_deliveries(db)
        db.commit()
    email = db.get(AlertDelivery, email_id)
    assert email.status == DeliveryStatus.failed
    assert email.attempts == notif.MAX_ATTEMPTS
    assert email.next_attempt_at is None
    # NOT lost: the in-app copy is still delivered/visible
    assert _deliveries(db, n.id)[AlertChannel.in_app].status == DeliveryStatus.delivered


def test_confirm_delivery_idempotent(db, make_school, make_staff):
    recipient = make_staff(make_school(), email="r@oakwood.edu")
    n = notif.enqueue(db, recipient_id=recipient.id, ntype="t", payload=None)
    db.commit()
    notif.process_due_deliveries(db)  # email -> sent
    db.commit()
    first = notif.confirm_delivery(db, n.id, delivered=True)
    db.commit()
    assert first is not None and first.status == DeliveryStatus.delivered
    assert first.delivered_at is not None
    second = notif.confirm_delivery(db, n.id, delivered=False)  # idempotent: stays delivered
    assert second is not None and second.status == DeliveryStatus.delivered


def test_confirm_rejects_skip_from_queued(db, make_school, make_staff):
    # queued -> delivered is illegal (must go through 'sent'); the webhook is ignored.
    recipient = make_staff(make_school(), email="r@oakwood.edu")
    n = notif.enqueue(db, recipient_id=recipient.id, ntype="t", payload=None)
    db.commit()
    result = notif.confirm_delivery(db, n.id, delivered=True)
    db.commit()
    assert result is not None and result.status == DeliveryStatus.queued  # skip rejected


def test_callback_failed_is_terminal(db, make_school, make_staff):
    recipient = make_staff(make_school(), email="r@oakwood.edu")
    n = notif.enqueue(db, recipient_id=recipient.id, ntype="t", payload=None)
    db.commit()
    notif.process_due_deliveries(db)  # -> sent
    db.commit()
    notif.confirm_delivery(db, n.id, delivered=False)  # -> failed (terminal)
    db.commit()
    late = notif.confirm_delivery(db, n.id, delivered=True)  # a late webhook can't un-fail it
    assert late is not None and late.status == DeliveryStatus.failed


def test_enqueue_endpoint_is_in_school_only(client, make_school, make_staff):
    school_a = make_school(code="A")
    school_b = make_school(code="B")
    producer = make_staff(school_a, email="p@oakwood.edu")
    other = make_staff(school_b, email="o@oakwood.edu")
    token = _token(client, "p@oakwood.edu")
    cross = client.post("/api/v1/notifications", json={"recipient_id": str(other.id), "type": "hi"}, headers=_auth(token))
    assert cross.status_code == 403
    ok = client.post("/api/v1/notifications", json={"recipient_id": str(producer.id), "type": "hi"}, headers=_auth(token))
    assert ok.status_code == 202


def test_feed_scopes_to_recipient_and_surfaces_context_and_channels(
    client, db, make_school, make_staff, monkeypatch
):
    monkeypatch.setattr(notif_mod, "send_email", _boom)
    school = make_school()
    me = make_staff(school, email="me@oakwood.edu")
    other = make_staff(school, email="other@oakwood.edu")
    notif.enqueue(db, recipient_id=me.id, ntype="mine", payload={"reason": "concern word: hurt"})
    notif.enqueue(db, recipient_id=other.id, ntype="theirs", payload=None)
    db.commit()
    notif.process_due_deliveries(db)  # my email fails -> retrying (surfaced, not lost)
    db.commit()
    feed = client.get("/api/v1/notifications", headers=_auth(_token(client, "me@oakwood.edu"))).json()
    types = {n["type"] for n in feed}
    assert "mine" in types and "theirs" not in types  # recipient-scoped
    entry = next(n for n in feed if n["type"] == "mine")
    # context preserved (M2 / NEG context) — not a bare notice
    assert entry["payload"]["reason"] == "concern word: hurt"
    statuses = {d["channel"]: d["status"] for d in entry["deliveries"]}
    # failed/retrying EMAIL is visible on the in-app surface (M1 / SC-054)
    assert statuses["in_app"] == "delivered"
    assert statuses["email"] in ("retrying", "failed")


def test_email_excludes_data_the_channel_should_not_carry(db):
    # NEG: the email body carries only the channel-appropriate reason, never raw payload/PII.
    n = Notification(
        recipient_id=uuid.uuid4(),
        type="alert",
        payload={"reason": "low mood", "student_name": "Amy Smith", "note": "SSN 123-45-6789"},
    )
    subject, body = notif._email_content(n)
    assert "low mood" in body  # actionable reason is allowed
    assert "Amy Smith" not in body and "123-45-6789" not in body  # PII stays in-app only


def test_delivery_webhook_requires_secret(client, db, make_school, make_staff, monkeypatch):
    from config.env_config import settings
    recipient = make_staff(make_school(), email="r@oakwood.edu")
    n = notif.enqueue(db, recipient_id=recipient.id, ntype="t", payload=None)
    db.commit()
    url = f"/api/v1/notifications/{n.id}/delivery"
    monkeypatch.setattr(settings, "sendgrid_webhook_secret", None)
    assert client.post(url, json={"delivered": True}).status_code == 503  # not configured
    monkeypatch.setattr(settings, "sendgrid_webhook_secret", "s3cret")
    assert client.post(url, json={"delivered": True}).status_code == 401  # wrong/missing secret
    ok = client.post(url, json={"delivered": True}, headers={"x-webhook-secret": "s3cret"})
    assert ok.status_code == 200


def test_worker_entrypoint_run_once(monkeypatch):
    # the runnable worker entrypoint runs ONE pass (its own session, commits, closes) — no loop.
    from src.application.notifications import worker

    calls: dict[str, bool] = {}

    class _FakeSession:
        def commit(self) -> None:
            calls["commit"] = True

        def close(self) -> None:
            calls["close"] = True

    monkeypatch.setattr(worker, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(worker, "process_due_deliveries", lambda db: 7)
    assert worker.run_once() == 7
    assert calls.get("commit") and calls.get("close")
