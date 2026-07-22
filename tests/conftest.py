"""Test harness — real Postgres (youhue_test), schema per session, truncate per test.

The FastAPI `get_db` dependency is overridden to share the test session so endpoint writes and
test assertions see the same data.
"""
from collections.abc import Callable, Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from alembic.config import Config
from config.db_connection import Base, get_db
from config.env_config import settings
from main import app
from src.application.auth.single_use import reset as reset_single_use
from src.constants.enums import (
    AdminRole,
    SchoolStatus,
    StaffClassScope,
    StaffRole,
    StaffStatus,
    StudentAgeBand,
)
from src.domain import registry as models  # noqa: F401  (register tables)
from src.domain.identity.models import InternalAdmin, School, StaffAccount, Student
from src.domain.org.models import ClassGroup, ClassMembership, StaffClassAccess
from src.infrastructure.middlewares.ratelimit import reset as reset_ratelimit
from src.utils.security import hash_password

_engine = create_engine(settings.database_url_test, future=True)
_TestSession = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
_ALEMBIC_INI = str(Path(__file__).resolve().parents[1] / "alembic.ini")


def _reset_schema() -> None:
    """Drop everything so the migration chain rebuilds the schema from a clean slate."""
    Base.metadata.drop_all(_engine)
    with _engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        conn.execute(text("DROP FUNCTION IF EXISTS youhue_block_mutation() CASCADE"))


@pytest.fixture(scope="session", autouse=True)
def _schema() -> Generator[None, None, None]:
    # Build the test schema by running the REAL Alembic migration chain (not create_all), so
    # model/migration drift fails the suite instead of passing green (INFRA-02 review fix). The
    # append-only triggers ship in migration c0ffee000001 and are applied here too.
    _reset_schema()
    cfg = Config(_ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", settings.database_url_test)
    command.upgrade(cfg, "head")
    yield
    _reset_schema()


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    session = _TestSession()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(autouse=True)
def _clean() -> Generator[None, None, None]:
    reset_ratelimit()  # in-process limiter must not leak across tests
    reset_single_use()  # in-process single-use jti registry must not leak across tests
    yield
    with _engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f'TRUNCATE TABLE "{table.name}" CASCADE'))


@pytest.fixture()
def client(db: Session) -> Generator[TestClient, None, None]:
    def _override() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def make_school(db: Session) -> Callable[..., School]:
    def _make(code: str = "SCH-001", status: SchoolStatus = SchoolStatus.active,
              name: str = "Oakwood") -> School:
        school = School(name=name, timezone="UTC", sign_in_code=code, status=status)
        db.add(school)
        db.commit()
        db.refresh(school)
        return school
    return _make


@pytest.fixture()
def make_staff(db: Session) -> Callable[..., StaffAccount]:
    def _make(school: School, email: str = "t@oakwood.edu", password: str | None = "Password123",
              role: StaffRole = StaffRole.teacher, mfa: bool = False) -> StaffAccount:
        staff = StaffAccount(
            school_id=school.id, email=email.lower(),
            password_hash=hash_password(password) if password else None,
            role=role, status=StaffStatus.active, mfa_enabled=mfa,
        )
        db.add(staff)
        db.commit()
        db.refresh(staff)
        return staff
    return _make


@pytest.fixture()
def make_admin(db: Session) -> Callable[..., InternalAdmin]:
    def _make(email: str = "admin@youhue.app", password: str | None = "Password123",
              role: AdminRole = AdminRole.superadmin) -> InternalAdmin:
        admin = InternalAdmin(
            email=email.lower(),
            password_hash=hash_password(password) if password else None,
            role=role,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
        return admin
    return _make


@pytest.fixture()
def make_student(db: Session) -> Callable[..., Student]:
    def _make(school: School, name: str = "Amy") -> Student:
        student = Student(school_id=school.id, display_name=name, age_band=StudentAgeBand.b8_11)
        db.add(student)
        db.commit()
        db.refresh(student)
        return student
    return _make


@pytest.fixture()
def make_class(db: Session) -> Callable[..., ClassGroup]:
    def _make(school: School, name: str = "3A") -> ClassGroup:
        klass = ClassGroup(school_id=school.id, name=name)
        db.add(klass)
        db.commit()
        db.refresh(klass)
        return klass
    return _make


@pytest.fixture()
def grant_class_access(db: Session) -> Callable[..., None]:
    def _grant(staff: StaffAccount, klass: ClassGroup,
               scope: StaffClassScope = StaffClassScope.owner) -> None:
        db.add(StaffClassAccess(staff_id=staff.id, class_id=klass.id, scope=scope))
        db.commit()
    return _grant


@pytest.fixture()
def add_to_class(db: Session) -> Callable[..., None]:
    def _add(klass: ClassGroup, student: Student) -> None:
        db.add(ClassMembership(class_id=klass.id, student_id=student.id))
        db.commit()
    return _add
