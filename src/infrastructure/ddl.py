"""Append-only trigger SQL for audit_logs + flag_events.

Applied to the test DB in conftest (after create_all) and to dev/prod via the matching Alembic
migration. A row can be INSERTed but never UPDATEd or DELETEd — protecting the audit trail.
"""

BLOCK_FN = (
    "CREATE OR REPLACE FUNCTION youhue_block_mutation() RETURNS trigger AS $$ "
    "BEGIN RAISE EXCEPTION 'append-only table %: % is not permitted', TG_TABLE_NAME, TG_OP; END; "
    "$$ LANGUAGE plpgsql;"
)


def trigger_sql(table: str) -> str:
    return (
        f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION youhue_block_mutation();"
    )
