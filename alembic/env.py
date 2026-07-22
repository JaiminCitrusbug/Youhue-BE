"""Alembic environment — wired to app settings + ORM metadata."""
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from config.env_config import settings
from src.domain import registry as models  # noqa: F401  (registers all tables on Base.metadata)
from config.db_connection import Base

config = context.config
# Respect a caller-supplied URL (e.g. the test harness pointing at youhue_test); otherwise the app's.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.database_url)
_db_url = config.get_main_option("sqlalchemy.url")

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=_db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, compare_type=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
