"""FR-14-02 — POST /api/v1/activities/{id}/run: a teacher runs a seed-set activity with their whole
class or assigns it to a specific student.

  Scenario 1  Run an activity with the whole class -> every roster member gets an `offered`
              ActivityEngagement.
              -> test_run_with_class_assigns_every_roster_member
  Scenario 2  Assign an activity to a specific student -> that one student gets an engagement.
              -> test_assign_to_student_assigns_just_that_student
  NEG         A class the caller has no access to (own school, different/unshared class) -> 403.
              -> test_403_for_class_outside_own_or_shared_scope
  NEG         A student outside the caller's own/shared class scope -> 403.
              -> test_403_for_student_outside_own_or_shared_scope
  NEG         Cross-school class/student target -> 403 (existence hidden across tenants).
              -> test_403_for_cross_school_class
  NEG         Unknown activity / class / student -> 404.
              -> test_404_for_unknown_activity
              -> test_404_for_unknown_class
              -> test_404_for_unknown_student_target
  NEG         A school-scoped (not seed) activity id -> 404 (drawn only from the seed set).
              -> test_404_for_school_scoped_activity_id
  NEG         A malformed target -> 422.
              -> test_422_for_malformed_target
  A co-teacher (support, shared-scope) can also run/assign on a class shared with them.
              -> test_shared_scope_support_role_can_run_with_class
  An empty class (no roster members) is a valid empty result, not an error.
              -> test_run_with_class_empty_roster_returns_empty_assigned
"""
import uuid

from sqlalchemy import select

from config.env_config import settings
from src.application.auth import sessions
from src.constants.enums import (
    ActivityAgeBand,
    ActivityEngagementStatus,
    ActivityScope,
    ActivityType,
    SessionKind,
    StaffClassScope,
    StaffRole,
)
from src.domain.checkin import services as checkin_db
from src.domain.checkin.models import Activity, ActivityEngagement
from src.domain.identity.models import StaffAccount

RUN = "/api/v1/activities/{id}/run"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint(db, staff: StaffAccount) -> str:
    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, settings.staff_session_ttl_minutes,
        school_id=staff.school_id,
    )
    db.commit()
    return sessions.issue_token(sess)


def _seed_activity(db, *, age_band: ActivityAgeBand = ActivityAgeBand.all) -> Activity:
    activity = checkin_db.add_seed_activity(
        db, title="Friendship circle", type=ActivityType.grounding, age_band=age_band, topic=None
    )
    db.commit()
    return activity


def test_run_with_class_assigns_every_roster_member(
    db, client, make_school, make_staff, make_class, make_student, grant_class_access, add_to_class
):
    school = make_school(code="RUN-1")
    teacher = make_staff(school, email="t@run1.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3A")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    a = make_student(school, name="Amy")
    b = make_student(school, name="Ben")
    add_to_class(klass, a)
    add_to_class(klass, b)
    activity = _seed_activity(db)

    token = _mint(db, teacher)
    r = client.post(
        RUN.format(id=activity.id), json={"target": f"class:{klass.id}"}, headers=_auth(token)
    )
    assert r.status_code == 200, r.text
    assigned = set(r.json()["assigned"])
    assert assigned == {str(a.id), str(b.id)}

    engagements = list(
        db.scalars(select(ActivityEngagement).where(ActivityEngagement.activity_id == activity.id))
    )
    assert len(engagements) == 2
    assert {e.student_id for e in engagements} == {a.id, b.id}
    assert all(e.status == ActivityEngagementStatus.offered for e in engagements)
    assert all(e.checkin_id is None for e in engagements)


def test_assign_to_student_assigns_just_that_student(
    db, client, make_school, make_staff, make_class, make_student, grant_class_access, add_to_class
):
    school = make_school(code="RUN-2")
    teacher = make_staff(school, email="t@run2.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3B")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    a = make_student(school, name="Amy")
    b = make_student(school, name="Ben")
    add_to_class(klass, a)
    add_to_class(klass, b)
    activity = _seed_activity(db)

    token = _mint(db, teacher)
    r = client.post(
        RUN.format(id=activity.id), json={"target": f"student:{a.id}"}, headers=_auth(token)
    )
    assert r.status_code == 200
    assert r.json()["assigned"] == [str(a.id)]

    engagements = list(
        db.scalars(select(ActivityEngagement).where(ActivityEngagement.activity_id == activity.id))
    )
    assert len(engagements) == 1
    assert engagements[0].student_id == a.id


def test_403_for_class_outside_own_or_shared_scope(
    db, client, make_school, make_staff, make_class
):
    school = make_school(code="RUN-3")
    teacher = make_staff(school, email="t@run3.edu", role=StaffRole.teacher)
    other_class = make_class(school, name="Other")  # teacher has NO access
    activity = _seed_activity(db)

    token = _mint(db, teacher)
    r = client.post(
        RUN.format(id=activity.id), json={"target": f"class:{other_class.id}"}, headers=_auth(token)
    )
    assert r.status_code == 403


def test_403_for_student_outside_own_or_shared_scope(
    db, client, make_school, make_staff, make_class, make_student, add_to_class
):
    school = make_school(code="RUN-4")
    teacher = make_staff(school, email="t@run4.edu", role=StaffRole.teacher)
    other_class = make_class(school, name="Other")  # teacher has NO access
    student = make_student(school, name="Cam")
    add_to_class(other_class, student)
    activity = _seed_activity(db)

    token = _mint(db, teacher)
    r = client.post(
        RUN.format(id=activity.id), json={"target": f"student:{student.id}"}, headers=_auth(token)
    )
    assert r.status_code == 403


def test_403_for_cross_school_class(db, client, make_school, make_staff, make_class):
    school_a = make_school(code="RUN-5A")
    school_b = make_school(code="RUN-5B")
    teacher = make_staff(school_a, email="t@run5.edu", role=StaffRole.teacher)
    other_school_class = make_class(school_b, name="Foreign")
    activity = _seed_activity(db)

    token = _mint(db, teacher)
    r = client.post(
        RUN.format(id=activity.id), json={"target": f"class:{other_school_class.id}"},
        headers=_auth(token),
    )
    assert r.status_code == 403  # never 404 — existence across tenants stays hidden


def test_404_for_unknown_activity(db, client, make_school, make_staff, make_class, grant_class_access):
    school = make_school(code="RUN-6")
    teacher = make_staff(school, email="t@run6.edu", role=StaffRole.teacher)
    klass = make_class(school)
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)

    token = _mint(db, teacher)
    r = client.post(
        RUN.format(id=uuid.uuid4()), json={"target": f"class:{klass.id}"}, headers=_auth(token)
    )
    assert r.status_code == 404


def test_404_for_unknown_class(db, client, make_school, make_staff):
    school = make_school(code="RUN-7")
    teacher = make_staff(school, email="t@run7.edu", role=StaffRole.teacher)
    activity = _seed_activity(db)

    token = _mint(db, teacher)
    r = client.post(
        RUN.format(id=activity.id), json={"target": f"class:{uuid.uuid4()}"}, headers=_auth(token)
    )
    assert r.status_code == 404


def test_404_for_unknown_student_target(db, client, make_school, make_staff):
    school = make_school(code="RUN-8")
    teacher = make_staff(school, email="t@run8.edu", role=StaffRole.teacher)
    activity = _seed_activity(db)

    token = _mint(db, teacher)
    r = client.post(
        RUN.format(id=activity.id), json={"target": f"student:{uuid.uuid4()}"}, headers=_auth(token)
    )
    assert r.status_code == 404


def test_404_for_school_scoped_activity_id(
    db, client, make_school, make_staff, make_class, grant_class_access
):
    """A school-authored (non-seed) activity is out of Phase-1 scope — `get_seed_activity` filters
    to scope=seed, so a school-scoped id resolves to None here, same 404 as unknown."""
    school = make_school(code="RUN-9")
    teacher = make_staff(school, email="t@run9.edu", role=StaffRole.teacher)
    klass = make_class(school)
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    school_activity = Activity(
        scope=ActivityScope.school, school_id=school.id, title="Local", type=ActivityType.stretch,
        age_band=ActivityAgeBand.all,
    )
    db.add(school_activity)
    db.commit()

    token = _mint(db, teacher)
    r = client.post(
        RUN.format(id=school_activity.id), json={"target": f"class:{klass.id}"},
        headers=_auth(token),
    )
    assert r.status_code == 404


def test_422_for_malformed_target(db, client, make_school, make_staff):
    school = make_school(code="RUN-10")
    teacher = make_staff(school, email="t@run10.edu", role=StaffRole.teacher)
    activity = _seed_activity(db)

    token = _mint(db, teacher)
    r = client.post(
        RUN.format(id=activity.id), json={"target": "not-a-valid-target"}, headers=_auth(token)
    )
    assert r.status_code == 422


def test_shared_scope_support_role_can_run_with_class(
    db, client, make_school, make_staff, make_class, make_student, grant_class_access, add_to_class
):
    school = make_school(code="RUN-11")
    co_teacher = make_staff(school, email="co@run11.edu", role=StaffRole.support)
    klass = make_class(school)
    grant_class_access(co_teacher, klass, scope=StaffClassScope.shared)
    student = make_student(school, name="Amy")
    add_to_class(klass, student)
    activity = _seed_activity(db)

    token = _mint(db, co_teacher)
    r = client.post(
        RUN.format(id=activity.id), json={"target": f"class:{klass.id}"}, headers=_auth(token)
    )
    assert r.status_code == 200
    assert r.json()["assigned"] == [str(student.id)]


def test_run_with_class_empty_roster_returns_empty_assigned(
    db, client, make_school, make_staff, make_class, grant_class_access
):
    school = make_school(code="RUN-12")
    teacher = make_staff(school, email="t@run12.edu", role=StaffRole.teacher)
    klass = make_class(school)  # no students added
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    activity = _seed_activity(db)

    token = _mint(db, teacher)
    r = client.post(
        RUN.format(id=activity.id), json={"target": f"class:{klass.id}"}, headers=_auth(token)
    )
    assert r.status_code == 200
    assert r.json()["assigned"] == []
