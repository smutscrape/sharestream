"""Unified share/media resolution.

Media and embed routes accept either an individual share id (``/share/{id}/...``)
or the composite tag-video id (``tag-{tag_share_id}-video-{video_id}``). Rather
than repeating ``if share_id.startswith("tag-") and "-video-" in share_id`` in
every such route, they call :func:`resolve_media`, which returns a single
:class:`ResolvedMedia` describing what the id points at and everything the
access gate + proxy need.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from sharestream.db.models import SharedTag, SharedVideo

KIND_INDIVIDUAL = "individual"
KIND_TAG_VIDEO = "tag_video"


@dataclass
class ResolvedMedia:
    """A resolved media/share access target.

    ``cookie_share_id`` is the id the password-unlock cookie is keyed to (the TAG
    share id for tag videos, otherwise the share id itself). ``share_id`` is the
    id as it appears in the request URL (the composite id for tag videos).
    """
    kind: str
    share_id: str
    cookie_share_id: str
    stash_video_id: int
    resolution: str
    expires_at: object
    password_hash: Optional[str]
    embed_mode: Optional[str]
    tag_share_id: Optional[str] = None
    stash_tag_id: Optional[str] = None
    title: Optional[str] = None
    # Whether the tag share is featured on the home gallery. Only meaningful for
    # tag videos; it (together with password_hash) decides whether the share's
    # media stays limited to limit_to_tag. Always False for individual shares.
    show_in_gallery: bool = False

    @property
    def is_tag_video(self) -> bool:
        return self.kind == KIND_TAG_VIDEO

    @property
    def is_individual(self) -> bool:
        return self.kind == KIND_INDIVIDUAL


def parse_composite_tag_video(share_id: str) -> Optional[tuple[str, int]]:
    """Return (tag_share_id, video_id) for a ``tag-<id>-video-<n>`` id, else None."""
    if not (share_id.startswith("tag-") and "-video-" in share_id):
        return None
    parts = share_id.split("-video-")
    if len(parts) != 2:
        return None
    tag_share_id = parts[0][4:]  # strip "tag-"
    try:
        video_id = int(parts[1])
    except (TypeError, ValueError):
        return None
    return tag_share_id, video_id


def resolve_media(db: Session, share_id: str) -> Optional[ResolvedMedia]:
    """Resolve ``share_id`` to a :class:`ResolvedMedia`, or None if unknown.

    Individual shares win first (an individual share id never starts with the
    reserved ``tag-...-video-...`` shape), then composite tag-video ids.
    """
    video = db.query(SharedVideo).filter(SharedVideo.share_id == share_id).first()
    if video:
        return ResolvedMedia(
            kind=KIND_INDIVIDUAL,
            share_id=share_id,
            cookie_share_id=share_id,
            stash_video_id=video.stash_video_id,
            resolution=video.resolution,
            expires_at=video.expires_at,
            password_hash=video.password_hash,
            embed_mode=video.embed_mode,
            title=video.video_name,
        )

    parsed = parse_composite_tag_video(share_id)
    if parsed:
        tag_share_id, video_id = parsed
        tag_share = db.query(SharedTag).filter(SharedTag.share_id == tag_share_id).first()
        if tag_share:
            return ResolvedMedia(
                kind=KIND_TAG_VIDEO,
                share_id=share_id,
                cookie_share_id=tag_share_id,
                stash_video_id=video_id,
                resolution=tag_share.resolution,
                expires_at=tag_share.expires_at,
                password_hash=tag_share.password_hash,
                embed_mode=tag_share.embed_mode,
                tag_share_id=tag_share_id,
                stash_tag_id=tag_share.stash_tag_id,
                title=None,
                show_in_gallery=tag_share.show_in_gallery,
            )

    return None
