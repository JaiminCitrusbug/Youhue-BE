"""FR-20-06: verifiable parental consent — one `parental_consents` row per student.

The `ParentalConsent` table itself (status ENUM pending|verified, captured_at TIMESTAMPTZ) already
exists — INFRA-02 (8ab7f8057e0a) scaffolded the full domain-entity set ahead of the FR tickets that
land on it. This ticket owns the business constraint the scaffold left open: "one per student"
(ticket §Data model). `ix_parental_consents_student_id` was a plain (non-unique) index; this makes
it UNIQUE so a second capture for the same student can only ever UPDATE the existing row, never
insert a duplicate (ACID + idempotent-on-retry, ticket Baseline BR-05).

Single alembic head (chains off FR-19-05). Note for the conductor (per ticket instructions): another
lane (FR-02-02) may also be adding a migration concurrently off the same tip — if `down_revision`
below no longer matches the tip at merge time, that's the known, handled re-chain pattern.

Revision ID: f20606a1b2c3
Revises: e5c7a1d9f204
Create Date: 2026-07-24 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f20606a1b2c3"
down_revision: Union[str, None] = "e5c7a1d9f204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_parental_consents_student_id", table_name="parental_consents")
    op.create_index(
        "ix_parental_consents_student_id", "parental_consents", ["student_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_parental_consents_student_id", table_name="parental_consents")
    op.create_index(
        "ix_parental_consents_student_id", "parental_consents", ["student_id"], unique=False
    )
