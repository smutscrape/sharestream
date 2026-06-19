"""SQLAlchemy ORM models and the Resolution enum.

Schema is intentionally unchanged from the original monolith. Columns added
after a table first shipped are handled by the idempotent migrations in
``db.migrations``.
"""
from __future__ import annotations

from enum import Enum

from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.orm import declarative_base

from sharestream.config import DEFAULT_RESOLUTION

Base = declarative_base()


class Resolution(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class SharedVideo(Base):
    __tablename__ = "shared_videos"
    id = Column(Integer, primary_key=True, index=True)
    share_id = Column(String, unique=True, index=True)
    video_name = Column(String)
    stash_video_id = Column(Integer)
    expires_at = Column(DateTime(timezone=True))
    hits = Column(Integer, default=0)
    resolution = Column(String, default=DEFAULT_RESOLUTION)
    password_hash = Column(String, nullable=True)
    show_in_gallery = Column(Boolean, default=False)
    embed_mode = Column(String, nullable=True)  # preview | full | dynamic | None(=config default)


class SharedTag(Base):
    __tablename__ = "shared_tags"
    id = Column(Integer, primary_key=True, index=True)
    share_id = Column(String, unique=True, index=True)
    tag_name = Column(String)
    stash_tag_id = Column(String)
    expires_at = Column(DateTime(timezone=True))
    hits = Column(Integer, default=0)
    resolution = Column(String, default=DEFAULT_RESOLUTION)
    password_hash = Column(String, nullable=True)
    show_in_gallery = Column(Boolean, default=False)
    embed_mode = Column(String, nullable=True)  # preview | full | dynamic | None(=config default)
    sort_order = Column(Integer, default=0)  # display order for the home "Collections" row & admin list
    default_sort = Column(String, nullable=True)  # date|title|hits|rating|duration|random | None(=config default)
    # For a NON-public share (password-protected OR not home-featured): whether the
    # global limit_to_tag filter is applied to this share's own surfaces. Featured
    # public shares always apply it regardless. New shares default to True; existing
    # rows are back-filled to False by the migration to preserve prior behavior.
    apply_limit_tag = Column(Boolean, default=True)


class TagVideoHit(Base):
    __tablename__ = "tag_video_hits"
    id = Column(Integer, primary_key=True, index=True)
    tag_share_id = Column(String, index=True)  # References SharedTag.share_id
    video_id = Column(Integer, index=True)      # Stash video ID
    hits = Column(Integer, default=0)
