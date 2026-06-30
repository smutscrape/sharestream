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
    apply_limit_tag = Column(Boolean, default=True)
    gallery_mode = Column(Boolean, default=False)


class TagVideoHit(Base):
    __tablename__ = "tag_video_hits"
    id = Column(Integer, primary_key=True, index=True)
    tag_share_id = Column(String, index=True)  # References SharedTag.share_id
    video_id = Column(Integer, index=True)      # Stash video ID
    hits = Column(Integer, default=0)


class VideoOverride(Base):
    """Per-video exceptions to the default Hashid-routed, tag-governed behavior.

    A row exists ONLY for a scene that needs something Stash can't store: a
    vanity slug and/or a password (with optional expiry), and now an optional
    custom display title for individual-share pages/admin.
    """
    __tablename__ = "video_overrides"
    id = Column(Integer, primary_key=True)
    stash_video_id = Column(Integer, unique=True, index=True, nullable=False)
    vanity_slug = Column(String, unique=True, index=True, nullable=True)
    password_hash = Column(String, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    custom_title = Column(String, nullable=True)


class SceneViews(Base):
    """Play counts keyed by Stash scene id rather than by share, so every entry
    path (canonical /v/, legacy redirects) contributes to one count. Supersedes
    SharedVideo.hits and TagVideoHit."""
    __tablename__ = "scene_views"
    stash_video_id = Column(Integer, primary_key=True)
    views = Column(Integer, default=0)
