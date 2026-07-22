"""Seed deterministic e2e fixtures into the DB the running app uses (DATABASE_URL).

Small, idempotent seed for the whole-product Playwright gate (approved deviation W-03):
one active school, one active teacher (password sign-in, no MFA), one active student.
IDs/codes are fixed so tests/e2e/whole-product.spec.ts can reference them.

Run from Youhue-BE with the app's .venv, pointing at the live DB, e.g.:
    $env:DATABASE_URL = "postgresql+psycopg://youhue:youhue@localhost:5433/youhue_test"
    .venv/Scripts/python.exe seed_e2e.py
"""
import uuid

from sqlalchemy import delete

from config.db_connection import SessionLocal, engine
from src.constants.enums import (
    AuthProvider,
    SchoolStatus,
    SchoolTier,
    StaffRole,
    StaffStatus,
    StudentAgeBand,
    StudentStatus,
)
from src.domain import registry as _models  # noqa: F401  register all tables
from src.domain.identity.models import School, StaffAccount, Student
from src.utils.security import hash_password

SCHOOL_ID = uuid.UUID("00000000-0000-0000-0000-0000000000e2")
STUDENT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
SCHOOL_CODE = "SCH-E2E"
STAFF_EMAIL = "teacher@oakwood.edu"
STAFF_PASSWORD = "Password123!"


def main() -> None:
    print(f"[seed] target DB: {engine.url}")
    with SessionLocal() as db:
        # idempotent: clear prior e2e rows for these keys
        db.execute(delete(Student).where(Student.id == STUDENT_ID))
        db.execute(delete(StaffAccount).where(StaffAccount.school_id == SCHOOL_ID))
        db.execute(delete(School).where(School.id == SCHOOL_ID))
        db.commit()

        school = School(
            id=SCHOOL_ID,
            name="Oakwood E2E",
            status=SchoolStatus.active,
            tier=SchoolTier.free,
            timezone="UTC",
            sign_in_code=SCHOOL_CODE,
        )
        db.add(school)

        staff = StaffAccount(
            school_id=SCHOOL_ID,
            email=STAFF_EMAIL.lower(),
            password_hash=hash_password(STAFF_PASSWORD),
            auth_provider=AuthProvider.password,
            role=StaffRole.teacher,
            status=StaffStatus.active,
            mfa_enabled=False,
        )
        db.add(staff)

        student = Student(
            id=STUDENT_ID,
            school_id=SCHOOL_ID,
            display_name="Amy E2E",
            age_band=StudentAgeBand.b8_11,
            status=StudentStatus.active,
        )
        db.add(student)
        db.commit()

    print("[seed] done:")
    print(f"  staff:   {STAFF_EMAIL} / {STAFF_PASSWORD} (role=teacher, active, no MFA)")
    print(f"  student: school_code={SCHOOL_CODE} student_id={STUDENT_ID}")


if __name__ == "__main__":
    main()
