"""Class-access issuance (FR-01-02).

Generates / rotates a class's PERSISTENT sign-in credentials: a human-typable join code + a
scannable qr_token. Both are persistent per class and change ONLY on an explicit rotate — there
is no auto-expiry (decision #2). FR-01-02 ships this service so the student flow is testable end
to end now; the teacher-facing UI that calls it is the separate ticket FR-01-09.
"""
import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.constants.enums import StaffClassScope
from src.domain.identity.models import StaffAccount
from src.domain.org import services as org_db
from src.schemas.classes import ClassOut, MyClassesResponse
from src.utils.security import new_join_code, new_url_token

_CLASS_NOT_FOUND = HTTPException(status.HTTP_404_NOT_FOUND, "Class not found")


@dataclass(frozen=True)
class ClassAccess:
    class_id: uuid.UUID
    join_code: str
    qr_token: str


def list_owned_classes(db: Session, staff: StaffAccount) -> MyClassesResponse:
    """FR-02-03: the caller's OWNED classes only (``StaffClassScope.owner``) — only a class owner
    may invite a colleague to share it (GATE gate: ``application.invitations.services``), so this
    is exactly the set ``InviteColleague.tsx``'s 'Shared class' picker needs — never a fixture."""
    rows = org_db.get_classes_for_staff(
        db, staff.id, staff.school_id, (StaffClassScope.owner,)
    )
    return MyClassesResponse(classes=[ClassOut(id=r.id, name=r.name) for r in rows])


def issue_access(db: Session, class_id: uuid.UUID) -> ClassAccess:
    """Return the class's persistent credentials, generating them once if not yet issued.

    Persistent: an already-issued class keeps its code + qr_token (only rotate changes them).
    """
    klass = org_db.get_class(db, class_id)
    if klass is None:
        raise _CLASS_NOT_FOUND
    if klass.join_code is not None and klass.qr_token is not None:
        return ClassAccess(class_id=klass.id, join_code=klass.join_code, qr_token=klass.qr_token)
    join_code, qr_token = new_join_code(), new_url_token()
    org_db.set_class_access(db, klass, join_code=join_code, qr_token=qr_token)
    return ClassAccess(class_id=klass.id, join_code=join_code, qr_token=qr_token)


def rotate_access(db: Session, class_id: uuid.UUID) -> ClassAccess:
    """Explicit rotation — replaces the code + qr_token, invalidating the previous pair."""
    klass = org_db.get_class(db, class_id)
    if klass is None:
        raise _CLASS_NOT_FOUND
    join_code, qr_token = new_join_code(), new_url_token()
    org_db.set_class_access(db, klass, join_code=join_code, qr_token=qr_token)
    return ClassAccess(class_id=klass.id, join_code=join_code, qr_token=qr_token)
