"""INFRA-06 risk pipeline: concern-word + slow-burn detectors, score + flag only (no band, no
student action), idempotent + dead-lettering worker, and the school-scoped /risk/score endpoint."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from config.env_config import settings
from src.application.derived import services as derived
from src.application.risk import services as risk
from src.constants.enums import FlagType
from src.domain.billing.models import Notification
from src.domain.checkin.models import CheckIn
from src.domain.risk.models import ConcernWordList, Flag

STAFF_SIGNIN = "/api/v1/auth/staff/sign-in"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _staff_token(client, email: str = "t@oakwood.edu") -> str:
    return client.post(STAFF_SIGNIN, json={"email": email, "password": "Password123"}).json()[
        "access_token"
    ]


def _mk_checkin(db, student, school, mood=3, reflection=None, when=None):
    c = CheckIn(
        student_id=student.id, school_id=school.id, mood_value=mood,
        reflection_text=reflection, submitted_at=when or datetime.now(UTC),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


# ---- concern-word detector -------------------------------------------------

def test_concern_word_flags_with_score_only_no_band(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    c = _mk_checkin(db, student, school, mood=3, reflection="I feel so alone today")
    result = risk.score_checkin(db, c)
    db.commit()
    assert result.flagged is True
    assert "alone" in result.matched_terms
    flag = db.query(Flag).first()
    assert flag.type == FlagType.concern_word
    assert float(flag.risk_score) == 0.90
    assert flag.band is None  # the pipeline never decides a band — FR-12-06 routes it


def test_school_override_list_used(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    db.add(ConcernWordList(school_id=school.id, words=["banana"], is_default=False))
    db.commit()
    c = _mk_checkin(db, student, school, reflection="i want a banana")
    assert "banana" in risk.score_checkin(db, c).matched_terms


def test_whole_word_match_no_substring_noise(db, make_school, make_student):
    # 'help' must not fire on 'helpful' — whole-word match limits noise (ticket §Must-nots)
    school = make_school()
    student = make_student(school)
    c = _mk_checkin(db, student, school, mood=5, reflection="a helpful cheerful morning")
    assert risk.score_checkin(db, c).flagged is False


def test_borderline_biases_toward_flagging(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    c = _mk_checkin(db, student, school, mood=5, reflection="i feel unsafe")  # good mood + concern
    assert risk.score_checkin(db, c).flagged is True


def test_clean_checkin_not_flagged(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    c = _mk_checkin(db, student, school, mood=5, reflection="great day")
    result = risk.score_checkin(db, c)
    db.commit()
    assert result.flagged is False
    assert db.query(Flag).count() == 0
    assert c.scored is True


def test_empty_school_wordlist_opts_out(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    db.add(ConcernWordList(school_id=school.id, words=[], is_default=False))
    db.commit()
    c = _mk_checkin(db, student, school, mood=5, reflection="i feel hopeless")  # a default word
    assert risk.score_checkin(db, c).flagged is False


# ---- slow-burn detector ----------------------------------------------------

def test_slow_burn_flags_after_low_streak(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    for i in range(settings.slowburn_window_days):
        _mk_checkin(db, student, school, mood=1, when=datetime.now(UTC) - timedelta(days=i))
    latest = _mk_checkin(db, student, school, mood=1)
    result = risk.score_checkin(db, latest)
    db.commit()
    assert result.flagged is True
    flag = db.query(Flag).filter(Flag.type == FlagType.slow_burn).first()
    assert flag is not None and float(flag.risk_score) == 0.70 and flag.band is None


def test_slow_burn_recovers_no_flag(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    for i in range(settings.slowburn_window_days):
        _mk_checkin(db, student, school, mood=1, when=datetime.now(UTC) - timedelta(days=i + 1))
    recovered = _mk_checkin(db, student, school, mood=5)  # latest risen back above threshold
    assert risk.score_checkin(db, recovered).flagged is False


def test_slow_burn_midwindow_blip_still_flags(db, make_school, make_student):
    # low across distinct days with ONE non-low blip mid-window (not the latest) -> still flags:
    # distinct-low-day counting biases toward flagging over missing, unlike an all()-must-be-low rule.
    school = make_school()
    student = make_student(school)
    _mk_checkin(db, student, school, mood=1, when=datetime.now(UTC) - timedelta(days=4))
    _mk_checkin(db, student, school, mood=5, when=datetime.now(UTC) - timedelta(days=3))  # blip
    _mk_checkin(db, student, school, mood=1, when=datetime.now(UTC) - timedelta(days=2))
    latest = _mk_checkin(db, student, school, mood=1)  # latest still low -> not recovered
    assert risk.score_checkin(db, latest).flagged is True


def test_slow_burn_single_day_does_not_flag(db, make_school, make_student):
    # many low check-ins but all on ONE day -> not a multi-day pattern -> no flag
    school = make_school()
    student = make_student(school)
    for _ in range(6):
        _mk_checkin(db, student, school, mood=1)
    assert risk.score_checkin(db, _mk_checkin(db, student, school, mood=1)).flagged is False


def test_slow_burn_day_count_is_configurable(db, make_school, make_student, monkeypatch):
    # the number of low days that trips a flag is env-driven, never hard-coded (ticket §Must-nots)
    monkeypatch.setattr(settings, "slowburn_min_low_days", 3)
    school = make_school()
    student = make_student(school)
    _mk_checkin(db, student, school, mood=1, when=datetime.now(UTC) - timedelta(days=1))
    latest = _mk_checkin(db, student, school, mood=1)  # only 2 distinct low days < configured 3
    assert risk.score_checkin(db, latest).flagged is False


# ---- score ownership, isolation, worker, thesis ----------------------------

def test_risk_score_owner_registered():
    assert derived.owner_name("flag.risk_score") == "combine_risk_score"


def test_concern_words_do_not_leak_across_schools(db, make_school, make_student):
    school_a = make_school(code="AAA-1", name="A")
    school_b = make_school(code="BBB-2", name="B")
    db.add(ConcernWordList(school_id=school_a.id, words=["banana"], is_default=False))
    db.commit()
    student_a = make_student(school_a)
    student_b = make_student(school_b)
    # A's override fires for A; B (no override -> default list) is never influenced by A's word
    assert risk.score_checkin(db, _mk_checkin(db, student_a, school_a, reflection="a banana")).flagged
    assert not risk.score_checkin(db, _mk_checkin(db, student_b, school_b, reflection="a banana")).flagged


def test_scoring_is_idempotent_on_retry(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    c = _mk_checkin(db, student, school, reflection="i feel worthless")
    first = risk.score_checkin(db, c)
    db.commit()
    second = risk.score_checkin(db, c)  # re-score the same check-in
    db.commit()
    assert first.flag_id == second.flag_id
    assert db.query(Flag).count() == 1  # no duplicate flag


def test_worker_scores_all_pending(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    _mk_checkin(db, student, school, reflection="i feel hopeless")
    _mk_checkin(db, student, school, reflection="ok")
    assert risk.process_pending(db) == 2
    db.commit()
    assert db.query(CheckIn).filter(CheckIn.scored.is_(False)).count() == 0


def test_scoring_error_retries_then_dead_letters(db, make_school, make_student, monkeypatch):
    import src.application.risk.services as risk_mod
    school = make_school()
    student = make_student(school)
    bad = _mk_checkin(db, student, school, reflection="i feel unsafe")
    good = _mk_checkin(db, student, school, reflection="ok")
    original = risk_mod.score_checkin

    def flaky(db_, c):
        if c.id == bad.id:
            raise RuntimeError("boom")
        return original(db_, c)

    monkeypatch.setattr(risk_mod, "score_checkin", flaky)
    # first pass: the good one is scored, the poison one is retried (not lost, not yet dead-lettered)
    risk.process_pending(db)
    db.commit()
    db.refresh(bad)
    db.refresh(good)
    assert good.scored is True
    assert bad.scored is False and bad.score_attempts == 1
    # keep going until the bounded cap dead-letters it — surfaced CRITICAL, never silently dropped
    for _ in range(settings.max_score_attempts):
        risk.process_pending(db)
        db.commit()
    db.refresh(bad)
    assert bad.scored is True  # dead-lettered
    assert db.query(CheckIn).filter(CheckIn.scored.is_(False)).count() == 0


def test_pipeline_takes_no_student_action(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    c = _mk_checkin(db, student, school, reflection="i feel worthless")
    risk.score_checkin(db, c)
    db.commit()
    assert db.query(Flag).count() == 1  # a flag is raised
    assert db.query(Notification).count() == 0  # but NOTHING is sent/acted (routing+alerting later)


# ---- POST /api/v1/risk/score (school-scoped internal endpoint) --------------

def test_score_endpoint_flags_and_is_idempotent(client, db, make_school, make_staff, make_student):
    school = make_school()
    make_staff(school)
    student = make_student(school)
    c = _mk_checkin(db, student, school, reflection="i feel hopeless")
    url = "/api/v1/risk/score"
    r = client.post(url, json={"checkin_id": str(c.id)}, headers=_auth(_staff_token(client)))
    assert r.status_code == 200
    body = r.json()
    assert body["flagged"] is True and body["risk_score"] == 0.90 and "hopeless" in body["matched_terms"]
    client.post(url, json={"checkin_id": str(c.id)}, headers=_auth(_staff_token(client)))
    assert db.query(Flag).count() == 1  # idempotent — no duplicate flag on a second call


@pytest.mark.authz
def test_score_endpoint_rejects_cross_school(client, db, make_school, make_staff, make_student):
    school_a = make_school(code="AAA-1", name="A")
    make_staff(school_a, email="a@oakwood.edu")
    school_b = make_school(code="BBB-2", name="B")
    student_b = make_student(school_b)
    c = _mk_checkin(db, student_b, school_b, reflection="i feel unsafe")
    r = client.post(
        "/api/v1/risk/score", json={"checkin_id": str(c.id)},
        headers=_auth(_staff_token(client, "a@oakwood.edu")),
    )
    assert r.status_code == 403  # a session never scores another school's check-in


def test_score_endpoint_unknown_checkin_404(client, make_school, make_staff):
    school = make_school()
    make_staff(school)
    r = client.post(
        "/api/v1/risk/score", json={"checkin_id": str(uuid.uuid4())},
        headers=_auth(_staff_token(client)),
    )
    assert r.status_code == 404


@pytest.mark.authz
def test_score_endpoint_denies_student_session(client, db, make_school, make_student):
    # scoring is internal/staff — a student session must not score (would expose a peer's terms)
    school = make_school(code="OAK-9")
    student = make_student(school)
    c = _mk_checkin(db, student, school, reflection="i feel unsafe")
    token = client.post(
        "/api/v1/auth/student/sign-in",
        json={"school_or_class_code": "OAK-9", "student_id": str(student.id)},
    ).json()["session_token"]
    r = client.post("/api/v1/risk/score", json={"checkin_id": str(c.id)}, headers=_auth(token))
    assert r.status_code == 403
