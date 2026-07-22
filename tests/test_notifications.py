"""INFRA-05 notification transport: dual-channel, retry+surface (no silent loss), feed, idempotency."""
import src.application.notifications as notif_mod
from src.application import notifications as notif
from src.domain.enums import AlertChannel, DeliveryStatus
from src.infrastructure.models.billing import Notification

SIGNIN = "/api/v1/auth/staff/sign-in"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(client, email: str) -> str:
    return client.post(SIGNIN, json={"email": email, "password": "Password123"}).json()["access_token"]


def _boom(to: str, subject: str, body: str) -> None:
    raise RuntimeError("smtp down")


def test_enqueue_delivers_both_channels(db, make_school, make_staff):
    recipient = make_staff(make_school(), email="r@oakwood.edu")
    rows = notif.enqueue(db, recipient_id=recipient.id, ntype="alert", payload={"reason": "concern word"})
    db.commit()
    assert {r.channel for r in rows} == {AlertChannel.in_app, AlertChannel.email}
    in_app = next(r for r in rows if r.channel == AlertChannel.in_app)
    assert in_app.delivery_status == DeliveryStatus.delivered  # stored immediately (redundancy)
    notif.dispatch_pending(db)
    db.commit()
    email = db.get(Notification, next(r.id for r in rows if r.channel == AlertChannel.email))
    assert email.delivery_status == DeliveryStatus.sent


def test_failed_send_retries_then_surfaces_not_lost(db, make_school, make_staff, monkeypatch):
    monkeypatch.setattr(notif_mod, "send_email", _boom)
    recipient = make_staff(make_school(), email="r@oakwood.edu")
    rows = notif.enqueue(db, recipient_id=recipient.id, ntype="alert", payload={"x": 1})
    db.commit()
    email_id = next(r.id for r in rows if r.channel == AlertChannel.email)
    for _ in range(notif.MAX_ATTEMPTS):
        notif.dispatch_pending(db)
        db.commit()
    email = db.get(Notification, email_id)
    assert email.delivery_status == DeliveryStatus.failed
    assert email.attempts == notif.MAX_ATTEMPTS
    # NOT lost: the in-app copy is still delivered/visible
    in_app_id = next(r.id for r in rows if r.channel == AlertChannel.in_app)
    assert db.get(Notification, in_app_id).delivery_status == DeliveryStatus.delivered


def test_retrying_before_exhaustion(db, make_school, make_staff, monkeypatch):
    monkeypatch.setattr(notif_mod, "send_email", _boom)
    recipient = make_staff(make_school(), email="r@oakwood.edu")
    rows = notif.enqueue(db, recipient_id=recipient.id, ntype="t", payload=None)
    db.commit()
    email_id = next(r.id for r in rows if r.channel == AlertChannel.email)
    notif.dispatch_pending(db)
    db.commit()
    email = db.get(Notification, email_id)
    assert email.delivery_status == DeliveryStatus.retrying
    assert email.attempts == 1


def test_confirm_delivery_idempotent(db, make_school, make_staff):
    recipient = make_staff(make_school(), email="r@oakwood.edu")
    rows = notif.enqueue(db, recipient_id=recipient.id, ntype="t", payload=None)
    db.commit()
    email_id = next(r.id for r in rows if r.channel == AlertChannel.email)
    first = notif.confirm_delivery(db, email_id, delivered=True)
    db.commit()
    assert first is not None and first.delivery_status == DeliveryStatus.delivered
    second = notif.confirm_delivery(db, email_id, delivered=False)  # idempotent: stays delivered
    assert second is not None and second.delivery_status == DeliveryStatus.delivered


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


def test_feed_returns_only_own_in_app(client, db, make_school, make_staff):
    school = make_school()
    me = make_staff(school, email="me@oakwood.edu")
    other = make_staff(school, email="other@oakwood.edu")
    notif.enqueue(db, recipient_id=me.id, ntype="mine", payload=None)
    notif.enqueue(db, recipient_id=other.id, ntype="theirs", payload=None)
    db.commit()
    feed = client.get("/api/v1/notifications", headers=_auth(_token(client, "me@oakwood.edu"))).json()
    types = {n["type"] for n in feed}
    assert "mine" in types and "theirs" not in types
    assert all(n["channel"] == "in_app" for n in feed)
