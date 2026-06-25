"""video_overrides and scene_views

Adds the two Phase-1 exception tables and migrates legacy data into them:

* ``video_overrides`` — one row per existing ``shared_videos`` row, preserving
  its slug as a vanity alias (so no existing /{slug} link breaks) plus any
  password/expiry.
* ``scene_views`` — per-scene play count = sum of ``shared_videos.hits`` and
  ``tag_video_hits.hits`` for that scene (mirrors the old get_total_plays_map
  aggregation across both legacy counters).

The data migration is forward-only; downgrade drops both tables.

Revision ID: 0002_overrides_views
Revises: 0001_initial
Create Date: 2026-06-24
"""
from __future__ import annotations

import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_overrides_views"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger(__name__)


def upgrade() -> None:
    op.create_table(
        "video_overrides",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stash_video_id", sa.Integer(), nullable=False),
        sa.Column("vanity_slug", sa.String(), nullable=True),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("video_overrides", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_video_overrides_stash_video_id"), ["stash_video_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_video_overrides_vanity_slug"), ["vanity_slug"], unique=True)

    op.create_table(
        "scene_views",
        sa.Column("stash_video_id", sa.Integer(), nullable=False),
        sa.Column("views", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("stash_video_id"),
    )

    _migrate_data()


def _migrate_data() -> None:
    bind = op.get_bind()

    # --- video_overrides: preserve every existing slug as a vanity alias ---
    # Keep the first row per stash_video_id (the unique index forbids dupes);
    # log any skipped alias so a rare duplicate-scene share isn't silently lost.
    seen: set[int] = set()
    rows = bind.execute(sa.text(
        "SELECT share_id, stash_video_id, password_hash, expires_at "
        "FROM shared_videos ORDER BY id"
    )).fetchall()
    for share_id, stash_video_id, password_hash, expires_at in rows:
        if stash_video_id is None:
            continue
        sid = int(stash_video_id)
        if sid in seen:
            logger.warning("Phase 1 migration: dropping duplicate vanity alias %r for scene %s", share_id, sid)
            continue
        seen.add(sid)
        bind.execute(
            sa.text(
                "INSERT INTO video_overrides (stash_video_id, vanity_slug, password_hash, expires_at) "
                "VALUES (:sid, :slug, :pw, :exp)"
            ),
            {"sid": sid, "slug": share_id, "pw": password_hash, "exp": expires_at},
        )

    # --- scene_views: sum of both legacy counters per scene ---
    totals: dict[int, int] = {}
    for stash_video_id, hits in bind.execute(sa.text(
        "SELECT stash_video_id, COALESCE(hits, 0) FROM shared_videos WHERE stash_video_id IS NOT NULL"
    )).fetchall():
        totals[int(stash_video_id)] = totals.get(int(stash_video_id), 0) + int(hits or 0)
    for video_id, hits in bind.execute(sa.text(
        "SELECT video_id, COALESCE(hits, 0) FROM tag_video_hits WHERE video_id IS NOT NULL"
    )).fetchall():
        totals[int(video_id)] = totals.get(int(video_id), 0) + int(hits or 0)
    for sid, views in totals.items():
        bind.execute(
            sa.text("INSERT INTO scene_views (stash_video_id, views) VALUES (:sid, :v)"),
            {"sid": sid, "v": views},
        )


def downgrade() -> None:
    op.drop_table("scene_views")
    with op.batch_alter_table("video_overrides", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_video_overrides_vanity_slug"))
        batch_op.drop_index(batch_op.f("ix_video_overrides_stash_video_id"))
    op.drop_table("video_overrides")
