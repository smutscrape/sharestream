"""Schema management via Alembic.

``run_migrations()`` replaces the old hand-rolled ``init_db()`` /
``_ensure_column`` bootstrap. It is called once at startup and:

1. Stamps a pre-existing, pre-Alembic database to the ``0001_initial`` baseline
   (whose schema is identical to what the old ``_ensure_column`` calls produced),
   so its data is preserved rather than re-created.
2. Runs ``alembic upgrade head`` — which creates every table on a fresh database
   and applies any later migrations on an existing one.
3. Seeds tag sort order (data, not schema — kept from the old bootstrap).

Safe to call on every startup.
"""
from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text

from sharestream.db.session import engine

logger = logging.getLogger(__name__)

_BASELINE_REVISION = "0001_initial"
# alembic.ini lives at the project root (the app's working directory).
_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _alembic_config() -> Config:
    return Config(str(_ALEMBIC_INI))


def _current_revision() -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _stamp_legacy_db_if_needed() -> None:
    """A database created by the old bootstrap has the app's tables but no
    ``alembic_version`` row. Stamp it to the baseline so ``upgrade head`` won't
    try to re-create existing tables (which would error)."""
    inspector = inspect(engine)
    has_tables = inspector.has_table("shared_tags")
    has_version = inspector.has_table("alembic_version")
    if has_tables and not has_version:
        logger.info("Pre-Alembic database detected; stamping to %s", _BASELINE_REVISION)
        command.stamp(_alembic_config(), _BASELINE_REVISION)


def _seed_tag_sort_order() -> None:
    """Seed sort_order for tag shares that don't have one yet, preserving their
    current insertion order (row id). Only seeds when NO custom order exists yet
    (every row at the default 0); once a reorder has happened (1-based orders),
    leave it alone so a restart doesn't clobber the operator's ordering."""
    with engine.connect() as conn:
        max_order = conn.execute(text("SELECT COALESCE(MAX(sort_order), 0) FROM shared_tags")).scalar()
        if not max_order:
            conn.execute(text("UPDATE shared_tags SET sort_order = id"))
            conn.commit()


def run_migrations() -> None:
    _stamp_legacy_db_if_needed()
    command.upgrade(_alembic_config(), "head")
    logger.info("Database at revision %s", _current_revision())
    _seed_tag_sort_order()
