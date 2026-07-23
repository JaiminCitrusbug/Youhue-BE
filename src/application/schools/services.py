"""School self-registration business logic (FR-02-01).

A teacher self-registers a new school. The school is created **pending** (SchoolStatus.pending is
the model default) and is NOT live — it cannot run student check-ins until a District/Trust admin
approves it (that decision is FR-02-02, explicitly out of scope here). The registering teacher is
associated as an active owner (StaffRole.teacher) so they can sign in and see the pending state.

Server-side validation is authoritative (the Pydantic schema rejects an empty name / non-RFC-5322
email with 422 before this runs). The whole create is one ACID unit — the router commits once — and
is idempotent on retry: a teacher who re-submits the same registration while their school is still
pending gets the same school back, never a duplicate (ticket §Interaction contract, BR-05).

Duplicate handling (ticket Scenarios):
  - A later teacher whose *email* already owns an **approved** school is routed to sign in (403).
  - A later teacher registering a name that matches an existing **approved** school joins it (409),
    rather than creating a duplicate school.

Structured logs: ``fr_02_01_success`` / ``_rejected`` / ``_forbidden`` / ``_error``.
"""
import logging

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.constants.enums import SchoolStatus
from src.domain.identity import services as identity_db
from src.schemas.schools import RegisterSchoolResponse
from src.utils import security

logger = logging.getLogger("youhue.schools")

_MAX_CODE_ATTEMPTS = 10


def register_school(
    db: Session, school_name: str, registrant_email: str, password: str
) -> RegisterSchoolResponse:
    name = school_name.strip()
    email = registrant_email.lower()

    # (1) Does this registrant already own an account? Retry-safe idempotency + member guard.
    existing_staff = identity_db.get_active_staff_by_email(db, email)
    if existing_staff is not None:
        school = identity_db.get_school(db, existing_staff.school_id)
        if school is not None and school.status == SchoolStatus.pending:
            # Idempotent replay of the registrant's own pending registration — no duplicate school.
            logger.info(
                "fr_02_01_success event=idempotent_replay school_id=%s status=pending", school.id
            )
            return RegisterSchoolResponse(school_id=school.id, status=SchoolStatus.pending.value)
        # They already belong to an approved (or decided) school — sign in, do not self-register.
        logger.warning(
            "fr_02_01_forbidden reason=already_member school_id=%s", existing_staff.school_id
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={
                "message": "This account already belongs to a school. Please sign in instead.",
                "school_id": str(existing_staff.school_id),
            },
        )

    # (2) Scenario 3: an APPROVED school of this name already exists — join it, never duplicate.
    existing_active = identity_db.get_active_school_by_name(db, name)
    if existing_active is not None:
        logger.info(
            "fr_02_01_rejected reason=duplicate_school school_id=%s", existing_active.id
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "message": "A school with this name already exists. Join it, do not duplicate.",
                "school_id": str(existing_active.id),
                "action": "join",
            },
        )

    # (3) Create the pending school + its owning teacher (one ACID unit; router commits once).
    code = _allocate_sign_in_code(db)
    school = identity_db.create_school(db, name=name, sign_in_code=code)
    identity_db.create_staff(
        db,
        school_id=school.id,
        email=email,
        password_hash=security.hash_password(password),
    )
    logger.info("fr_02_01_success event=created school_id=%s status=pending", school.id)
    return RegisterSchoolResponse(school_id=school.id, status=SchoolStatus.pending.value)


def _allocate_sign_in_code(db: Session) -> str:
    """A globally-unique, human-typable school sign-in code. Collision-checked before use; the DB
    unique constraint remains the final backstop."""
    for _ in range(_MAX_CODE_ATTEMPTS):
        code = security.new_school_code()
        if not identity_db.sign_in_code_exists(db, code):
            return code
    logger.error("fr_02_01_error reason=sign_in_code_exhausted")
    raise HTTPException(
        status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not allocate a school sign-in code"
    )
