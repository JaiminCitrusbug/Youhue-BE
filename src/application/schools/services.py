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

--------------------------------------------------------------------------------------------------
FR-02-02 (District/Trust approval) lives in this module too — same tenant, same router file. See
the functions below (``list_pending_schools`` / ``get_school_detail`` / ``decide_school``) and their
own docstrings. Structured logs: ``fr_02_02_success`` / ``_rejected`` / ``_forbidden`` / ``_error``.
"""
import logging
import re
import uuid
from typing import Literal

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.application.authz import services as authz
from src.application.isolation import services as isolation
from src.application.staff_lifecycle import services as staff_lifecycle
from src.constants.enums import SchoolStatus, StaffRole, StaffStatus
from src.domain.billing import services as billing_db
from src.domain.identity import services as identity_db
from src.domain.identity.models import School, StaffAccount
from src.schemas.schools import (
    PendingSchoolOut,
    PendingSchoolsResponse,
    RegisterSchoolResponse,
    SchoolDecisionResponse,
    SchoolDetailResponse,
)
from src.utils import security

logger = logging.getLogger("youhue.schools")

_NOT_PENDING = HTTPException(
    status.HTTP_409_CONFLICT,
    detail={
        "code": "not_pending",
        "message": "This school is not awaiting approval (already decided).",
    },
)

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


# ================================================================================================
# FR-02-02 — District/Trust leadership reviews + approves/rejects a pending school
# ================================================================================================
#
# RBAC: reuses the INFRA-03 role gate as-is (``authz.require_roles``), the same primitive every
# other role-scoped feature calls — no parallel policy layer invented (ticket instruction: follow
# the ``AdminDep``/staff-role-guard shape already in the codebase). The actor is a ``StaffAccount``
# with ``StaffRole.district`` — reached via the ordinary ``StaffDep`` (so the actor's OWN school
# must be active and their own account active), NOT ``require_same_school`` — a District/Trust
# reviewer acts on OTHER schools' pending registrations by design, so the cross-tenant guard does
# not apply here.
#
# KNOWN GAP (logged, not silently reconciled): the data model has no Trust/District-to-school
# mapping (no "which trust owns which pending school" table exists anywhere in INFRA-01/02). Any
# staff account with ``role=district`` can therefore decide on ANY pending school platform-wide,
# not only "schools requesting to join this trust" as SC-069's copy implies. Introducing real
# trust-scoping is a schema + product decision outside this ticket's stated scope (it only asks
# for the district/leadership role check) and is left for a follow-up ticket.
#
# READ ENDPOINTS (list + detail) are NOT in the ticket's "What to build" (only the decision POST is
# named), but the FE DoD requires SC-069/SC-070 wired to real data with "no dead controls" — a list
# screen and a review screen cannot do that against zero read endpoints. Logged, not silently
# reconciled: two minimal, district-gated GET endpoints are added so the FE has something real to
# call, symmetrical with the decision endpoint's own RBAC gate.


def list_pending_schools(db: Session, actor: StaffAccount) -> PendingSchoolsResponse:
    """GET /schools/pending — the District approval queue (SC-069), oldest first."""
    try:
        authz.require_roles(actor, StaffRole.district)
    except HTTPException:
        logger.warning(
            "fr_02_02_forbidden action=list actor_id=%s role=%s", actor.id, actor.role.value
        )
        raise
    rows = []
    for school in identity_db.list_pending_schools(db):
        registrant = identity_db.get_registrant_of_school(db, school.id)
        rows.append(
            PendingSchoolOut(
                school_id=school.id,
                name=school.name,
                registrant_email=registrant.email if registrant else None,
                created_at=school.created_at,
            )
        )
    return PendingSchoolsResponse(schools=rows)


def get_school_detail(
    db: Session, actor: StaffAccount, school_id: uuid.UUID
) -> SchoolDetailResponse:
    """GET /schools/{id} — the single-school review view (SC-070)."""
    try:
        authz.require_roles(actor, StaffRole.district)
    except HTTPException:
        logger.warning(
            "fr_02_02_forbidden action=read actor_id=%s role=%s school_id=%s",
            actor.id, actor.role.value, school_id,
        )
        raise
    school = identity_db.get_school(db, school_id)
    if school is None:
        logger.info("fr_02_02_rejected action=read reason=not_found school_id=%s", school_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "School not found")
    registrant = identity_db.get_registrant_of_school(db, school.id)
    return SchoolDetailResponse(
        school_id=school.id,
        name=school.name,
        status=school.status.value,
        registrant_email=registrant.email if registrant else None,
        student_count=identity_db.count_students_in_school(db, school.id),
        created_at=school.created_at,
    )


def decide_school(
    db: Session, actor: StaffAccount, school_id: uuid.UUID, decision: str
) -> SchoolDecisionResponse:
    """POST /schools/{id}/decision — approve or reject a pending school. One ACID transaction
    (the router commits once, on success); every branch that raises leaves nothing written the
    router has not already been told to roll back.

    Approve:
      * ``School.status`` -> ``active`` (the school goes live — GATE G-1 stops blocking check-ins);
      * the registrant (and any other still-``invited`` staff at the school) -> ``active``
        (FR-02-01's documented follow-on: "FR-02-02 activates the school and its registrant
        together");
      * a ``Subscription`` row is armed: ``tier=premium``, ``state=trial``, ``trial_start_at=None``
        — the 6-week whole-school Premium trial clock does NOT start here (FR-17-03, first
        check-in).

    Reject:
      * ``School.status`` -> ``rejected`` — recorded, terminal; the school stays not-live. No
        ``Subscription`` row is created (nothing to arm for a school that will never go live from
        this registration; ``uq_schools_name_live`` also releases the name for a fresh attempt).

    409 (idempotency/re-decision guard [BR-05]): the school is locked ``FOR UPDATE`` and re-checked
    for ``pending`` inside the transaction, so two concurrent decisions on the same school cannot
    both succeed — the loser sees the already-decided status, never re-arms a trial or re-flips a
    rejection.
    """
    try:
        authz.require_roles(actor, StaffRole.district)
    except HTTPException:
        logger.warning(
            "fr_02_02_forbidden action=decide actor_id=%s role=%s school_id=%s",
            actor.id, actor.role.value, school_id,
        )
        raise

    school = identity_db.get_school_for_decision(db, school_id)
    if school is None:
        logger.info("fr_02_02_rejected action=decide reason=not_found school_id=%s", school_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "School not found")
    if school.status != SchoolStatus.pending:
        logger.info(
            "fr_02_02_rejected action=decide reason=not_pending school_id=%s status=%s",
            school_id, school.status.value,
        )
        raise _NOT_PENDING

    result_status: Literal["active", "rejected"]
    if decision == "approve":
        school.status = SchoolStatus.active
        result_status = "active"
        # FR-02-04: walk each still-invited account through the real lifecycle graph (invited ->
        # sent -> accepted -> active) rather than jumping straight to active — the same
        # `staff_lifecycle.advance_to` mechanism `invitations.services.accept_invitation` uses, so
        # every onboarding path (self-registration+approval, colleague invite) shares ONE state
        # machine, not two independent ad-hoc mutations.
        for invited in identity_db.list_invited_staff_in_school(db, school.id):
            staff_lifecycle.advance_to(db, invited, StaffStatus.active)
        billing_db.arm_trial_subscription(db, school.id)
        isolation.audit(
            db, actor_id=actor.id, action="school.approved", target=str(school.id),
            school_id=school.id,
        )
        logger.info(
            "fr_02_02_success event=approved school_id=%s actor_id=%s", school.id, actor.id
        )
    else:
        school.status = SchoolStatus.rejected
        result_status = "rejected"
        isolation.audit(
            db, actor_id=actor.id, action="school.rejected", target=str(school.id),
            school_id=school.id,
        )
        logger.info(
            "fr_02_02_success event=rejected school_id=%s actor_id=%s", school.id, actor.id
        )

    db.flush()
    return SchoolDecisionResponse(school_id=school.id, status=result_status)
