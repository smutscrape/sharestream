"""Lightweight, idempotent schema bootstrap + migrations.

``init_db()`` creates tables, adds any columns introduced after a table first
shipped (SQLite's create_all won't add columns to an existing table), and seeds
the tag display order. Safe to call on every startup.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from sharestream.db.models import Base
from sharestream.db.session import engine

logger = logging.getLogger(__name__)


def _ensure_column(table: str, column: str, ddl_type: str):
    with engine.connect() as conn:
        existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
        if column not in existing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
            conn.commit()
            logger.info(f"Migrated: added column {table}.{column}")


def _seed_tag_sort_order():
    """Seed sort_order for any tag shares that don't have one yet (preserve their
    current insertion order by falling back to the row id)."""
    with engine.connect() as conn:
        # Only seed when NO custom order exists yet (every row still at the
        # default 0). Once any reorder has happened (orders are 1-based), leave
        # it alone — otherwise a restart would clobber the user's ordering.
        max_order = conn.execute(text("SELECT COALESCE(MAX(sort_order), 0) FROM shared_tags")).scalar()
        if not max_order:
            conn.execute(text("UPDATE shared_tags SET sort_order = id"))
            conn.commit()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_column("shared_videos", "embed_mode", "VARCHAR")
    _ensure_column("shared_tags", "embed_mode", "VARCHAR")
    _ensure_column("shared_tags", "sort_order", "INTEGER DEFAULT 0")
    _ensure_column("shared_tags", "default_sort", "VARCHAR")
    # Back-fill to 0 (False), NOT the model default of True: non-public shares
    # created before this column existed have always BYPASSED limit_to_tag, so
    # newly applying it on restart could 404 videos those shares legitimately
    # serve. New shares get True from the ORM default; only pre-existing rows are
    # pinned to the old behavior here.
    _ensure_column("shared_tags", "apply_limit_tag", "BOOLEAN DEFAULT 0")
    _seed_tag_sort_order()
