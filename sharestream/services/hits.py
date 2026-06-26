"""Hit counters for shares and per-tag videos.

Centralized so the increments can later be made atomic (e.g. an UPDATE ...
SET hits = hits + 1 or an upsert) for safe multi-worker operation. Today they
keep the original read-modify-write-commit behavior.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from sharestream.db.models import SceneViews, SharedTag, SharedVideo, TagVideoHit


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


def increment_scene_view(db: Session, stash_video_id: int) -> int:
    """Increment the unified per-scene view counter and return the new total.

    Uses an upsert so concurrent views from different watch entry points
    (``/v/{slug}``, ``/{gallery}/{sqid}``, ``/{slug}``) can't lose increments.
    The counter is keyed by Stash scene id, so every entry path contributes to
    one shared total that surfaces everywhere counts are displayed.
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    sid = int(stash_video_id)
    # Try an atomic INSERT ... ON CONFLICT DO UPDATE (upsert). The exact
    # SQL varies by backend, so we pick the dialect at runtime. Fallback to
    # the portable read-modify-write if neither dialect is available.
    dialect_name = db.bind.dialect.name if db.bind else ""
    if dialect_name == "postgresql":
        stmt = pg_insert(SceneViews).values(stash_video_id=sid, views=1)
        stmt = stmt.on_conflict_do_update(
            index_elements=[SceneViews.stash_video_id],
            set_={"views": SceneViews.views + 1},
        )
        db.execute(stmt)
        db.commit()
    elif dialect_name == "sqlite":
        stmt = sqlite_insert(SceneViews).values(stash_video_id=sid, views=1)
        stmt = stmt.on_conflict_do_update(
            index_elements=[SceneViews.stash_video_id],
            set_={"views": SceneViews.views + 1},
        )
        db.execute(stmt)
        db.commit()
    else:
        # Portable fallback: read-modify-write within the open transaction.
        row = db.query(SceneViews).filter(
            SceneViews.stash_video_id == sid).with_for_update().first()
        if row is None:
            row = SceneViews(stash_video_id=sid, views=1)
            db.add(row)
        else:
            row.views = (row.views or 0) + 1
        db.commit()
    row = db.query(SceneViews).filter(SceneViews.stash_video_id == sid).first()
    return row.views if row else 0


def get_total_plays_map(db: Session, video_ids) -> dict[int, int]:
    """Return ``{stash_video_id: total_plays}`` from the unified per-scene counter.

    All watch entry points contribute to the same ``SceneViews`` row per
    scene, so this single query yields the authoritative count shown on every
    surface (home, tag galleries, the by-tag-name gallery, video pages) and
    used for the "Play Count" sort. Missing ids default to 0.
    """
    ids = {int(v) for v in video_ids}
    if not ids:
        return {}
    totals: dict[int, int] = {vid: 0 for vid in ids}
    for stash_video_id, views in db.query(
            SceneViews.stash_video_id, SceneViews.views).filter(
            SceneViews.stash_video_id.in_(ids)).all():
        totals[int(stash_video_id)] = views or 0
    return totals


def get_total_plays(db: Session, video_id: int) -> int:
    """Total plays for one Stash scene from the unified per-scene counter (see
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
