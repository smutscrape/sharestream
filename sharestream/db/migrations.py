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

def _backfill_video_overrides_from_shared_videos() -> None:
    """Idempotently create missing VideoOverride rows from SharedVideo rows.

    Phase 0002 migrated the legacy SharedVideo rows that existed at the time,
    but later code paths (notably old filedrop auto-share) continued inserting
    new SharedVideo rows. Those short URLs now flow through the legacy redirect
    shim and lose the password flow. Backfilling a VideoOverride with the SAME
    slug/password/expiry makes /{share_id}?pwd=... work again.

    Safe to call on every startup:
    * rows for scenes that already have a VideoOverride are skipped
    * duplicate SharedVideo rows for one scene are skipped after the first
    * vanity-slug collisions are skipped and logged
    """
    with engine.begin() as conn:
        shared_rows = conn.execute(text(
            "SELECT id, share_id, stash_video_id, password_hash, expires_at "
            "FROM shared_videos "
            "WHERE stash_video_id IS NOT NULL "
            "ORDER BY id"
        )).mappings().all()

        if not shared_rows:
            return

        override_rows = conn.execute(text(
            "SELECT stash_video_id, vanity_slug FROM video_overrides"
        )).mappings().all()

        existing_by_scene = {
            int(r["stash_video_id"]): (r["vanity_slug"] or "")
            for r in override_rows
        }
        existing_slugs = {
            str(r["vanity_slug"])
            for r in override_rows
            if r["vanity_slug"]
        }

        created = 0
        skipped_scene = 0
        skipped_slug = 0
        seen_new_scenes: set[int] = set()

        for row in shared_rows:
            sid = int(row["stash_video_id"])
            slug = (row["share_id"] or "").strip() or None

            # If this scene already has a modern override, leave it alone.
            if sid in existing_by_scene or sid in seen_new_scenes:
                existing_slug = existing_by_scene.get(sid) or ""
                if slug and existing_slug and existing_slug != slug:
                    logger.warning(
                        "VideoOverride backfill: SharedVideo slug %r for scene %s "
                        "skipped; existing VideoOverride already uses %r",
                        slug, sid, existing_slug,
                    )
                skipped_scene += 1
                continue

            # Extremely rare, but don't steal a slug that is already in use by
            # some other override.
            if slug and slug in existing_slugs:
                logger.warning(
                    "VideoOverride backfill: SharedVideo slug %r for scene %s "
                    "skipped; vanity slug already taken",
                    slug, sid,
                )
                skipped_slug += 1
                continue

            conn.execute(text(
                "INSERT INTO video_overrides "
                "(stash_video_id, vanity_slug, password_hash, expires_at) "
                "VALUES (:sid, :slug, :pw, :exp)"
            ), {
                "sid": sid,
                "slug": slug,
                "pw": row["password_hash"],
                "exp": row["expires_at"],
            })

            seen_new_scenes.add(sid)
            if slug:
                existing_slugs.add(slug)
            created += 1

    if created or skipped_scene or skipped_slug:
        logger.info(
            "VideoOverride backfill complete: created=%s skipped_scene=%s skipped_slug=%s",
            created, skipped_scene, skipped_slug,
        )

def run_migrations() -> None:
    _stamp_legacy_db_if_needed()
    command.upgrade(_alembic_config(), "head")
    logger.info("Database at revision %s", _current_revision())
    _seed_tag_sort_order()