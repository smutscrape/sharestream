"""Alembic migration environment.

Wires Alembic to the app's own engine and metadata so migrations always target
the same database the app uses (``sharestream.config.DATABASE_URL``) and
autogenerate compares against ``Base.metadata``. ``render_as_batch=True`` is
required for SQLite: it rewrites ALTER TABLE operations (drop/alter column,
constraint changes) as the create-copy-drop dance SQLite needs.
"""
from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from sharestream.config import DATABASE_URL
from sharestream.db.models import Base

config = context.config
# Default to the app's configured database, but let a caller (tests, CLI with
# -x, a programmatic Config) override by setting sqlalchemy.url first.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", DATABASE_URL)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
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
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
