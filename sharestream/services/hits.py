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


def get_total_plays_map(db: Session, video_ids) -> dict[int, int]:
    """Return ``{stash_video_id: total_plays}`` aggregated across BOTH counters.

    A single Stash scene can be viewed through several share contexts — one or
    more individual shares (``SharedVideo.hits``) and any number of tag shares
    (``TagVideoHit.hits``, keyed per tag share). Each context tracks its own
    tally, but a viewer's "play count" for the underlying video is the sum of
    them all. This is the single number shown on every surface (home, tag
    galleries, the by-tag-name gallery, the video pages) and used for the
    "Play Count" sort, so the same video reads consistently everywhere.

    Two indexed ``IN`` queries; ids with no recorded views default to 0 at the
    call site. Multiple individual shares of the same scene are summed.
    """
    ids = {int(v) for v in video_ids}
    if not ids:
        return {}
    totals: dict[int, int] = {vid: 0 for vid in ids}
    for stash_video_id, hits in db.query(
            SharedVideo.stash_video_id, SharedVideo.hits).filter(
            SharedVideo.stash_video_id.in_(ids)).all():
        totals[int(stash_video_id)] = totals.get(int(stash_video_id), 0) + (hits or 0)
    for video_id, hits in db.query(
            TagVideoHit.video_id, TagVideoHit.hits).filter(
            TagVideoHit.video_id.in_(ids)).all():
        totals[int(video_id)] = totals.get(int(video_id), 0) + (hits or 0)
    return totals


def get_total_plays(db: Session, video_id: int) -> int:
    """Total plays for one Stash scene across every share context (see
    :func:`get_total_plays_map`)."""
    return get_total_plays_map(db, [video_id]).get(int(video_id), 0)


def get_tag_video_hits_map(db: Session, tag_share_id: str) -> dict[int, int]:
    """Return {video_id: hits} for every tracked video in a tag share, in ONE query.

    Gallery builders need a hit count per card. Doing that per-video is a classic
    N+1 — for a hits-sorted view of a large tag it was one SELECT *per scene in
    the whole tag* (thousands of round-trips). Only videos that have actually been
    viewed have a row, so this result is small (<= number of videos ever opened),
    and missing ids simply default to 0 at the call site.
    """
    rows = db.query(TagVideoHit.video_id, TagVideoHit.hits).filter(
        TagVideoHit.tag_share_id == tag_share_id
    ).all()
    return {video_id: hits for video_id, hits in rows}


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
