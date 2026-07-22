"""INFRA-06 risk pipeline: concern-word + slow-burn detectors, band routing, async worker,
and the thesis — the pipeline surfaces signal and NEVER acts on a student."""
from datetime import UTC, datetime, timedelta

from config.env_config import settings
from src.application.derived import services as derived
from src.application.risk import services as risk
from src.constants.enums import FlagBand, FlagType
from src.domain.billing.models import Notification
from src.domain.checkin.models import CheckIn
from src.domain.risk.models import ConcernWordList, Flag


def _mk_checkin(db, student, school, mood=3, reflection=None, when=None):
    c = CheckIn(
        student_id=student.id, school_id=school.id, mood_value=mood,
        reflection_text=reflection, submitted_at=when or datetime.now(UTC),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def test_concern_word_flags_immediate(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    c = _mk_checkin(db, student, school, mood=3, reflection="I feel so alone today")
    result = risk.score_checkin(db, c)
    db.commit()
    assert result.flagged is True
    assert "alone" in result.matched_terms
    flag = db.query(Flag).first()
    assert flag.type == FlagType.concern_word
    assert flag.band == FlagBand.immediate  # 0.90 >= 0.80


def test_school_override_list_used(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    db.add(ConcernWordList(school_id=school.id, words=["banana"], is_default=False))
    db.commit()
    c = _mk_checkin(db, student, school, reflection="i want a banana")
    assert "banana" in risk.score_checkin(db, c).matched_terms


def test_borderline_biases_toward_flagging(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    c = _mk_checkin(db, student, school, mood=5, reflection="i feel unsafe")  # good mood but concern word
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


def test_slow_burn_flags_after_consecutive_low(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    for i in range(settings.slowburn_window_days):
        _mk_checkin(db, student, school, mood=1, when=datetime.now(UTC) - timedelta(days=i))
    latest = _mk_checkin(db, student, school, mood=1)
    result = risk.score_checkin(db, latest)
    db.commit()
    assert result.flagged is True
    flag = db.query(Flag).filter(Flag.type == FlagType.slow_burn).first()
    assert flag is not None and flag.band == FlagBand.triage  # 0.70 in [0.50, 0.80)


def test_slow_burn_recovers_no_flag(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    for i in range(settings.slowburn_window_days):
        _mk_checkin(db, student, school, mood=1, when=datetime.now(UTC) - timedelta(days=i + 1))
    recovered = _mk_checkin(db, student, school, mood=5)  # risen back above threshold
    assert risk.score_checkin(db, recovered).flagged is False


def test_route_band_boundaries():
    assert risk.route_band(0.80) == FlagBand.immediate
    assert risk.route_band(0.79) == FlagBand.triage
    assert risk.route_band(0.50) == FlagBand.triage
    assert risk.route_band(0.49) == FlagBand.none


def test_worker_scores_all_pending(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    _mk_checkin(db, student, school, reflection="i feel hopeless")
    _mk_checkin(db, student, school, reflection="ok")
    assert risk.process_pending(db) == 2
    db.commit()
    assert db.query(CheckIn).filter(CheckIn.scored.is_(False)).count() == 0


def test_flag_band_owner_registered():
    assert derived.owner_name("flag.band") == "route_band"


def test_pipeline_takes_no_student_action(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    c = _mk_checkin(db, student, school, reflection="i feel worthless")
    risk.score_checkin(db, c)
    db.commit()
    assert db.query(Flag).count() == 1  # a flag is raised
    assert db.query(Notification).count() == 0  # but NOTHING is sent/acted (routing+alerting is later)


def test_slow_burn_flags_with_a_missed_day(db, make_school, make_student):
    # low on 4 distinct days (one day missed) -> still flags: counts DISTINCT DAYS, not rows (M3 fix)
    school = make_school()
    student = make_student(school)
    for i in (1, 2, 4):
        _mk_checkin(db, student, school, mood=1, when=datetime.now(UTC) - timedelta(days=i))
    latest = _mk_checkin(db, student, school, mood=1)
    assert risk.score_checkin(db, latest).flagged is True


def test_slow_burn_single_day_does_not_flag(db, make_school, make_student):
    # many low check-ins but all on ONE day -> not a multi-day pattern -> no flag (M3 fix)
    school = make_school()
    student = make_student(school)
    for _ in range(6):
        _mk_checkin(db, student, school, mood=1)
    assert risk.score_checkin(db, _mk_checkin(db, student, school, mood=1)).flagged is False


def test_empty_school_wordlist_opts_out(db, make_school, make_student):
    # a school that clears its list opts OUT of concern-word detection (M8 fix)
    school = make_school()
    student = make_student(school)
    db.add(ConcernWordList(school_id=school.id, words=[], is_default=False))
    db.commit()
    c = _mk_checkin(db, student, school, reflection="i feel hopeless")  # a default word
    assert risk.score_checkin(db, c).flagged is False


def test_scoring_error_dead_letters_without_poisoning_batch(db, make_school, make_student, monkeypatch):
    import src.application.risk.services as risk_mod
    school = make_school()
    student = make_student(school)
    bad = _mk_checkin(db, student, school, reflection="i feel unsafe")
    _mk_checkin(db, student, school, reflection="ok")
    original = risk_mod.score_checkin

    def flaky(db_, c):
        if c.id == bad.id:
            raise RuntimeError("boom")
        return original(db_, c)

    monkeypatch.setattr(risk_mod, "score_checkin", flaky)
    risk.process_pending(db)
    db.commit()
    # the bad one is dead-lettered and the good one is NOT lost -> nothing stuck unscored (B2 fix)
    assert db.query(CheckIn).filter(CheckIn.scored.is_(False)).count() == 0
