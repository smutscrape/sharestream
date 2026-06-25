"""initial schema

Baseline matching the schema shipped by the hand-rolled init_db()/_ensure_column
bootstrap: shared_videos, shared_tags (with sort_order/default_sort/
apply_limit_tag/gallery_mode), and tag_video_hits. Existing databases are
stamped to this revision at startup rather than re-created; fresh databases get
these tables created by `upgrade head`.

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-24
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shared_tags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("share_id", sa.String(), nullable=True),
        sa.Column("tag_name", sa.String(), nullable=True),
        sa.Column("stash_tag_id", sa.String(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hits", sa.Integer(), nullable=True),
        sa.Column("resolution", sa.String(), nullable=True),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("show_in_gallery", sa.Boolean(), nullable=True),
        sa.Column("embed_mode", sa.String(), nullable=True),
        # server_default of a bare `0` (sa.text, not "0" which renders quoted)
        # mirrors the `DEFAULT 0` DDL the old _ensure_column path emitted, so a
        # fresh DB is byte-for-byte identical to a stamped legacy DB.
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("default_sort", sa.String(), nullable=True),
        sa.Column("apply_limit_tag", sa.Boolean(), server_default=sa.text("0"), nullable=True),
        sa.Column("gallery_mode", sa.Boolean(), server_default=sa.text("0"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("shared_tags", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_shared_tags_id"), ["id"], unique=False)
        batch_op.create_index(batch_op.f("ix_shared_tags_share_id"), ["share_id"], unique=True)

    op.create_table(
        "shared_videos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("share_id", sa.String(), nullable=True),
        sa.Column("video_name", sa.String(), nullable=True),
        sa.Column("stash_video_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hits", sa.Integer(), nullable=True),
        sa.Column("resolution", sa.String(), nullable=True),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("show_in_gallery", sa.Boolean(), nullable=True),
        sa.Column("embed_mode", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("shared_videos", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_shared_videos_id"), ["id"], unique=False)
        batch_op.create_index(batch_op.f("ix_shared_videos_share_id"), ["share_id"], unique=True)

    op.create_table(
        "tag_video_hits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tag_share_id", sa.String(), nullable=True),
        sa.Column("video_id", sa.Integer(), nullable=True),
        sa.Column("hits", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("tag_video_hits", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_tag_video_hits_id"), ["id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tag_video_hits_tag_share_id"), ["tag_share_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tag_video_hits_video_id"), ["video_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("tag_video_hits", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tag_video_hits_video_id"))
        batch_op.drop_index(batch_op.f("ix_tag_video_hits_tag_share_id"))
        batch_op.drop_index(batch_op.f("ix_tag_video_hits_id"))
    op.drop_table("tag_video_hits")

    with op.batch_alter_table("shared_videos", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_shared_videos_share_id"))
        batch_op.drop_index(batch_op.f("ix_shared_videos_id"))
    op.drop_table("shared_videos")

    with op.batch_alter_table("shared_tags", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_shared_tags_share_id"))
        batch_op.drop_index(batch_op.f("ix_shared_tags_id"))
    op.drop_table("shared_tags")
