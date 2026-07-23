"""FR-02-01: DB backstop for school self-registration uniqueness

One partial unique index that the application read-then-write cannot provide (review M3):

* ``uq_schools_name_live``  — at most one non-rejected school per case-insensitive name, so two
  concurrent registrations of the same school cannot both create one. Rejected schools are excluded
  so a rejected registration's name is released for a fresh attempt.

This is the whole duplicate-school Must-not guarantee. Staff email is deliberately NOT made
globally unique: the schema allows one email to hold an active account at more than one school
(``uq_staff_school_email`` is per-school by design; FR-01-03 SSO account-linking relies on it), and
FR-02-01's registrant is created non-active, so it cannot introduce an ambiguous active row.

The expression index (``lower(name)``) is created with raw SQL because Alembic's ``create_index``
does not express a functional index portably. It is re-creatable and non-destructive.

Revision ID: e1c7b40a9d38
Revises: b3d9f1a4c7e2
Create Date: 2026-07-23 16:40:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1c7b40a9d38"
down_revision: Union[str, None] = "b3d9f1a4c7e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_schools_name_live ON schools (lower(name)) "
        "WHERE status <> 'rejected'::school_status"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_schools_name_live")
