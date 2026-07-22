"""Current-identity (`/me`) business logic. DB via domain services."""
from sqlalchemy.orm import Session

from src.constants.enums import SessionKind
from src.domain.auth.models import AuthSession
from src.domain.identity import services as identity_db
from src.schemas.auth import MeResponse


def resolve_me(db: Session, sess: AuthSession) -> MeResponse:
    role: str | None = None
    if sess.kind == SessionKind.staff:
        staff = identity_db.get_staff(db, sess.subject_id)
        role = staff.role.value if staff else None
    return MeResponse(
        subject_id=sess.subject_id, kind=sess.kind.value, school_id=sess.school_id, role=role
    )
