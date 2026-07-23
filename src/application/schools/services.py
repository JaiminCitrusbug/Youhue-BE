"""School self-registration business logic (FR-02-01).

A teacher self-registers a new school. Two things are created, and NEITHER is live:

  * the **school**, ``SchoolStatus.pending`` — it cannot run student check-ins (all three student
    resolution paths filter ``status == active``);
  * the **registrant**, ``StaffStatus.invited`` — deliberately NOT an active account. Staff sign-in
    resolves accounts with ``get_active_staff_by_email``, so an unapproved registrant cannot obtain
    a session, and therefore cannot reach any side-effecting staff endpoint, on an email nobody has
    verified. "Pending" means pending on every surface, not just the student one. A second,
    independent guard enforces the same invariant for any session however it was minted
    (``get_current_staff`` / ``staff.sign_in`` both require a live school).

  FR-02-02 (approval) activates the school and its registrant together; that is where the
  registrant becomes usable. Email is unique only per school (a teacher may legitimately be active
  at more than one school — FR-01-03), so no global-email guard is imposed here.

Non-disclosure is a hard requirement — this is the product's first unauthenticated public write:

  * The branch taken is decided by the SCHOOL NAME, never by whether the email is known, so the
    endpoint answers no question about who has a Youhue account. An email that is already staff
    elsewhere registers a new school exactly like an unknown one does.
  * Every path performs exactly one bcrypt (hash on create, verify on conflict), so the outcome is
    not readable from the response time either.
  * The 409 names no tenant UUID and does not distinguish a pending school from a live one.

Duplicate handling (ticket Must-not: "must not create a duplicate school"):
  * The name is matched case-insensitively and whitespace-normalised against every non-rejected
    school, and ``uq_schools_name_live`` is the DB backstop the read-then-write cannot be — two
    concurrent registrations of one name cannot both win.
  * Only the registrant who already owns that pending registration, proven by their password, gets
    it back (idempotent retry, [BR-05]). Everyone else — including a wrong password on a real
    account — gets the same terminal 409, so registration can neither be replayed by a stranger nor
    used to squat an email behind a silent success.

Known residual (recorded, not silently accepted): duplicate detection is name equality after
normalisation, so "Greenfield High" and "Greenfield High School" remain two schools. Fuzzy/token
matching was rejected here because a false match permanently REFUSES a legitimate school and this
ticket ships no override path. The durable fix is a real identity key (DfE URN / domain /
postcode) captured at registration and reviewed at approval — that is a schema + product decision
and belongs with FR-02-02, where a human already inspects the queue.

Structured logs: ``fr_02_01_success`` / ``_rejected`` / ``_forbidden`` / ``_error``.
"""
import logging
import re

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.constants.enums import SchoolStatus, StaffStatus
from src.domain.identity import services as identity_db
from src.domain.identity.models import School
from src.schemas.schools import RegisterSchoolResponse
from src.utils import security

logger = logging.getLogger("youhue.schools")

_MAX_CODE_ATTEMPTS = 10
_WHITESPACE = re.compile(r"\s+")

# One terminal answer for every "this name is taken" outcome, whoever the caller is. No school_id,
# no join action (there is no join endpoint — FR-02-03 is not built), no pending-vs-live hint.
_SCHOOL_EXISTS = HTTPException(
    status.HTTP_409_CONFLICT,
    detail={
        "code": "school_exists",
        "message": (
            "A school with this name is already registered with Youhue. "
            "Ask a colleague at your school to invite you."
        ),
    },
)


def _normalise_name(raw: str) -> str:
    """Trim and collapse internal whitespace, so "  Oakwood   Primary " and "Oakwood Primary" are
    one school. Matching on top of this is case-insensitive — exactly what ``uq_schools_name_live``
    (``lower(name)``) enforces in the DB."""
    return _WHITESPACE.sub(" ", raw).strip()


def register_school(
    db: Session, school_name: str, registrant_email: str, password: str
) -> RegisterSchoolResponse:
    name = _normalise_name(school_name)
    email = registrant_email.lower()

    existing = identity_db.get_school_by_name(db, name)
    if existing is not None:
        return _replay_or_conflict(db, existing, email, password)
    return _create_pending_school(db, name, email, password)


def _replay_or_conflict(
    db: Session, school: School, email: str, password: str
) -> RegisterSchoolResponse:
    """The name is taken. Give it back ONLY to the registrant who can prove they own the pending
    registration; every other caller gets the identical 409."""
    staff = identity_db.get_staff_by_email_in_school(db, email, school.id)
    # Always one bcrypt against a real-shaped hash -> the answer is not readable from the timing.
    stored = staff.password_hash if staff and staff.password_hash else security.DUMMY_PASSWORD_HASH
    password_ok = security.verify_password(password, stored)

    if staff is not None and password_ok:
        if school.status == SchoolStatus.pending:
            # Idempotent retry of this registrant's own registration — never a duplicate school.
            logger.info(
                "fr_02_01_success event=idempotent_replay school_id=%s status=pending", school.id
            )
            return RegisterSchoolResponse(school_id=school.id)
        logger.info("fr_02_01_rejected reason=already_registered school_id=%s", school.id)
    elif staff is not None:
        # A member of this school failed to prove it. An ops/intrusion signal — but the caller sees
        # exactly what a stranger sees, so it is not an oracle.
        logger.warning("fr_02_01_forbidden reason=credential_mismatch school_id=%s", school.id)
    else:
        logger.info("fr_02_01_rejected reason=duplicate_school school_id=%s", school.id)
    raise _SCHOOL_EXISTS


def _create_pending_school(
    db: Session, name: str, email: str, password: str
) -> RegisterSchoolResponse:
    """Create the pending school + its not-yet-active registrant. One ACID unit — both rows flush
    here and the router commits once, so no half-registered school can survive a failure."""
    code = _allocate_sign_in_code(db)
    try:
        school = identity_db.create_school(db, name=name, sign_in_code=code)
        identity_db.create_staff(
            db,
            school_id=school.id,
            email=email,
            password_hash=security.hash_password(password),
            status=StaffStatus.invited,  # not usable until FR-02-02 approves the school
        )
    except IntegrityError as exc:
        # A concurrent registration won the race to this name (or this sign-in code). The DB
        # constraint is the guarantee the application read above can never be; same answer either
        # way, so the race is invisible to both callers.
        logger.info("fr_02_01_rejected reason=duplicate_school event=race")
        raise _SCHOOL_EXISTS from exc
    logger.info("fr_02_01_success event=created school_id=%s status=pending", school.id)
    return RegisterSchoolResponse(school_id=school.id)


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
