"""Hit counters for shares and per-tag videos.

Centralized so the increments can later be made atomic (e.g. an UPDATE ...
SET hits = hits + 1 or an upsert) for safe multi-worker operation. Today they
keep the original read-modify-write-commit behavior.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from sharestream.db.models import SceneViews, SharedTag, TagVideoHit


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


def increment_tag_hit(db: Session, tag: SharedTag) -> int:
    """Count a view of a tag share page and return the new total (atomic)."""
    db.query(SharedTag).filter(SharedTag.id == tag.id).update(
        {SharedTag.hits: SharedTag.hits + 1}, synchronize_session=False)
    db.commit()
    db.refresh(tag)
    return tag.hits
