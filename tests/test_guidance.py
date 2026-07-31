"""FR-13-04: guided response — GET /api/v1/flags/{id}/guidance.

Advisory only (suggested wording, next steps, useful links) for the teacher opening the response
to a flagged check-in: use/adapt/ignore, never forced. No new persistence — reads the existing
Flag only. Read restricted to the INVOLVED teacher (reuses FR-10-02's `authz.require_student_access`
own/shared-class-scope check), 403 otherwise; school-scoped.
"""
import uuid

from src.application.risk import services as risk
from src.constants.enums import FlagBand, FlagType, StaffClassScope
from src.domain.risk import services as risk_db
from src.domain.risk.models import Flag

STAFF_SIGNIN = "/api/v1/auth/staff/sign-in"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _staff_token(client, email: str = "t@oakwood.edu") -> str:
    return client.post(STAFF_SIGNIN, json={"email": email, "password": "Password123"}).json()[
        "access_token"
    ]


def _mk_flag(db, student, school, flag_type=FlagType.concern_word, band=None) -> Flag:
    flag = risk_db.create_flag(
        db, student_id=student.id, school_id=school.id, checkin_id=None,
        flag_type=flag_type, risk_score=0.90, band=band,
    )
    db.commit()
    db.refresh(flag)
    return flag


def _involved_setup(db, make_school, make_staff, make_student, make_class, grant_class_access,
                     add_to_class, *, flag_type=FlagType.concern_word, band=None):
    school = make_school()
    teacher = make_staff(school)
    student = make_student(school)
    klass = make_class(school)
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    add_to_class(klass, student)
    flag = _mk_flag(db, student, school, flag_type=flag_type, band=band)
    return school, teacher, student, flag


# ---- Scenario 1: guidance offered when opening the response --------------------------------

def test_guidance_offers_wording_steps_and_links(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    _, _, _, flag = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    r = client.get(
        f"/api/v1/flags/{flag.id}/guidance", headers=_auth(_staff_token(client)),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["suggested_wording"]
    assert isinstance(body["next_steps"], list) and len(body["next_steps"]) > 0
    assert isinstance(body["links"], list) and len(body["links"]) > 0
    assert all("label" in link for link in body["links"])


# ---- Scenario 2: suggestions are advisory only — nothing forces/requires an action ----------

def test_guidance_is_advisory_only_no_gating_field(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    """The response carries no required/gate/status field — the schema itself cannot force
    acting on a suggestion; repeat reads return the same advisory content with no state change."""
    _, _, _, flag = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    token = _staff_token(client)
    r1 = client.get(f"/api/v1/flags/{flag.id}/guidance", headers=_auth(token))
    r2 = client.get(f"/api/v1/flags/{flag.id}/guidance", headers=_auth(token))
    assert r1.status_code == 200 and r2.status_code == 200
    assert set(r1.json().keys()) == {"suggested_wording", "next_steps", "links"}
    assert r1.json() == r2.json()  # pure/derived — repeat opens never mutate or vary


def test_guidance_no_new_db_writes(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    """DoD: 'no new persistence for guidance' — row counts across every risk table are unchanged
    by the GET call."""
    from src.domain.billing.models import Notification
    from src.domain.risk.models import AlertDelivery, FlagEvent

    _, _, _, flag = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    before = (
        db.query(Flag).count(), db.query(FlagEvent).count(),
        db.query(AlertDelivery).count(), db.query(Notification).count(),
    )
    r = client.get(f"/api/v1/flags/{flag.id}/guidance", headers=_auth(_staff_token(client)))
    assert r.status_code == 200
    after = (
        db.query(Flag).count(), db.query(FlagEvent).count(),
        db.query(AlertDelivery).count(), db.query(Notification).count(),
    )
    assert before == after


# ---- permission: restricted to the involved teacher ------------------------------------------

def test_guidance_403_for_uninvolved_teacher(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    school, _, _, flag = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    make_staff(school, email="other@oakwood.edu")  # no class access to the student
    r = client.get(
        f"/api/v1/flags/{flag.id}/guidance", headers=_auth(_staff_token(client, "other@oakwood.edu")),
    )
    assert r.status_code == 403


def test_guidance_403_cross_school(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    _, _, _, flag = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    school_b = make_school(code="BBB-4", name="B4")
    make_staff(school_b, email="b4@oakwood.edu")
    r = client.get(
        f"/api/v1/flags/{flag.id}/guidance", headers=_auth(_staff_token(client, "b4@oakwood.edu")),
    )
    assert r.status_code == 403


def test_guidance_unknown_flag_404(client, make_school, make_staff):
    school = make_school()
    make_staff(school)
    r = client.get(
        f"/api/v1/flags/{uuid.uuid4()}/guidance", headers=_auth(_staff_token(client)),
    )
    assert r.status_code == 404


def test_guidance_denies_student_session(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    school, _, student, flag = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    # student sessions never carry a StaffAccount -> StaffDep rejects them before the handler runs
    token = client.post(
        "/api/v1/auth/student/sign-in",
        json={"school_or_class_code": school.sign_in_code, "student_id": str(student.id)},
    ).json()["session_token"]
    r = client.get(f"/api/v1/flags/{flag.id}/guidance", headers=_auth(token))
    assert r.status_code == 403


# ---- structured logs: fr_13_04_success / _forbidden -------------------------------------------

def test_guidance_success_logs_fr_13_04_success(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    monkeypatch,
):
    calls: list[str] = []
    monkeypatch.setattr(risk.logger, "info", lambda msg, *a, **k: calls.append(msg % a if a else msg))
    _, _, _, flag = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    r = client.get(f"/api/v1/flags/{flag.id}/guidance", headers=_auth(_staff_token(client)))
    assert r.status_code == 200
    assert any(c.startswith("fr_13_04_success") for c in calls)


def test_guidance_forbidden_logs_fr_13_04_forbidden(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    monkeypatch,
):
    calls: list[str] = []
    monkeypatch.setattr(
        risk.logger, "warning", lambda msg, *a, **k: calls.append(msg % a if a else msg)
    )
    school, _, _, flag = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    make_staff(school, email="other2@oakwood.edu")
    r = client.get(
        f"/api/v1/flags/{flag.id}/guidance",
        headers=_auth(_staff_token(client, "other2@oakwood.edu")),
    )
    assert r.status_code == 403
    assert any(c.startswith("fr_13_04_forbidden") and "reason=not_involved" in c for c in calls)


# ---- pure content derivation: build_guidance(flag) --------------------------------------------

def test_build_guidance_varies_by_flag_type(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    concern = _mk_flag(db, student, school, flag_type=FlagType.concern_word)
    slow_burn = _mk_flag(db, student, school, flag_type=FlagType.slow_burn)
    assert risk.build_guidance(concern).suggested_wording != risk.build_guidance(slow_burn).suggested_wording


def test_build_guidance_immediate_band_adds_urgency_step(db, make_school, make_student):
    school = make_school()
    # two distinct students -> uq_flags_open_student_type (student_id, type) isn't tripped by
    # having the same flag_type on both open flags.
    triage = _mk_flag(db, make_student(school, name="A"), school, band=FlagBand.triage)
    immediate = _mk_flag(db, make_student(school, name="B"), school, band=FlagBand.immediate)
    assert len(risk.build_guidance(immediate).next_steps) > len(risk.build_guidance(triage).next_steps)
