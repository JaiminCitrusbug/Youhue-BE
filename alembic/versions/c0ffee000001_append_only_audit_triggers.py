"""audit_logs + flag_events append-only triggers (immutability)

Revision ID: c0ffee000001
Revises: 15dc6e10375a
"""
from alembic import op

revision = "c0ffee000001"
down_revision = "15dc6e10375a"
branch_labels = None
depends_on = None

_FN = (
    "CREATE OR REPLACE FUNCTION youhue_block_mutation() RETURNS trigger AS $$ "
    "BEGIN RAISE EXCEPTION 'append-only table %: % is not permitted', TG_TABLE_NAME, TG_OP; END; "
    "$$ LANGUAGE plpgsql;"
)


def upgrade() -> None:
    op.execute(_FN)
    op.execute(
        "CREATE TRIGGER audit_logs_append_only BEFORE UPDATE OR DELETE ON audit_logs "
        "FOR EACH ROW EXECUTE FUNCTION youhue_block_mutation();"
    )
    op.execute(
        "CREATE TRIGGER flag_events_append_only BEFORE UPDATE OR DELETE ON flag_events "
        "FOR EACH ROW EXECUTE FUNCTION youhue_block_mutation();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_logs_append_only ON audit_logs;")
    op.execute("DROP TRIGGER IF EXISTS flag_events_append_only ON flag_events;")
    op.execute("DROP FUNCTION IF EXISTS youhue_block_mutation();")
