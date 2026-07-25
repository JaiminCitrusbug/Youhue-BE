"""FR-04-03 (age-band check-in config, ticket DoD): "Data model: Student.age_band (ENUM
b5_7|b8_11|b12_18) indexed and used to resolve config." The column already existed (FR-03-01
roster import) but carried no index — `GET /api/v1/check-ins/config` reads it once per request
(via the already-loaded `student` dependency, no extra query), so this index is not load-bearing
for that endpoint's own query plan; it exists to satisfy the ticket's explicit data-model
requirement and to support any future admin/reporting query that filters/groups students by band.

Revision ID: a3f0c9d21e77
Revises: c4d8f2a91b7e
Create Date: 2026-07-25 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f0c9d21e77"
down_revision: Union[str, None] = "c4d8f2a91b7e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_students_age_band", "students", ["age_band"])


def downgrade() -> None:
    op.drop_index("ix_students_age_band", table_name="students")
