"""Hit counters for shares and per-tag videos.

Centralized so the increments can later be made atomic (e.g. an UPDATE ...
SET hits = hits + 1 or an upsert) for safe multi-worker operation. Today they
keep the original read-modify-write-commit behavior.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from sharestream.db.models import SharedTag, SharedVideo, TagVideoHit


def get_or_create_tag_video_hit(db: Session, tag_share_id: str, video_id: int) -> TagVideoHit:
    """Get or create a TagVideoHit record for tracking hits on individual videos
    within tag shares."""
    hit_record = db.query(TagVideoHit).filter(
        TagVideoHit.tag_share_id == tag_share_id,
        TagVideoHit.video_id == video_id
    ).first()

    if not hit_record:
        hit_record = TagVideoHit(
            tag_share_id=tag_share_id,
            video_id=video_id,
            hits=0
        )
        db.add(hit_record)
        db.commit()

    return hit_record


def get_tag_video_hit(db: Session, tag_share_id: str, video_id: int) -> TagVideoHit | None:
    """Read the hit record for a tag video (or None if not tracked yet)."""
    return db.query(TagVideoHit).filter(
        TagVideoHit.tag_share_id == tag_share_id,
        TagVideoHit.video_id == video_id
    ).first()


def increment_share_hit(db: Session, video: SharedVideo) -> int:
    """Count a view of an individual share and return the new total.

    Uses an atomic ``UPDATE ... SET hits = hits + 1`` so concurrent views can't
    lose increments (read-modify-write would). ``refresh`` then reloads the
    post-increment value for display.
    """
    db.query(SharedVideo).filter(SharedVideo.id == video.id).update(
        {SharedVideo.hits: SharedVideo.hits + 1}, synchronize_session=False)
    db.commit()
    db.refresh(video)
    return video.hits


def increment_tag_hit(db: Session, tag: SharedTag) -> int:
    """Count a view of a tag share page and return the new total (atomic)."""
    db.query(SharedTag).filter(SharedTag.id == tag.id).update(
        {SharedTag.hits: SharedTag.hits + 1}, synchronize_session=False)
    db.commit()
    db.refresh(tag)
    return tag.hits


def increment_tag_video_hit(db: Session, tag_share_id: str, video_id: int) -> TagVideoHit:
    """Count a view of a video within a tag share and return its hit record.

    The row is created if needed, then incremented atomically.
    """
    hit_record = get_or_create_tag_video_hit(db, tag_share_id, video_id)
    db.query(TagVideoHit).filter(TagVideoHit.id == hit_record.id).update(
        {TagVideoHit.hits: TagVideoHit.hits + 1}, synchronize_session=False)
    db.commit()
    db.refresh(hit_record)
    return hit_record
