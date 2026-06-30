"""Admin panel routes: panel redirect, listings, reordering, cache clear, and
the Stash video-title lookup used by the admin UI. All are JWT-protected except
the static redirect."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse


from sqlalchemy.orm import Session

from sharestream.backends.stash import fetch_scene_title, get_scene_titles, search_scene_titles
from sharestream.config import BASE_DOMAIN, GALLERY_MASONRY_DEFAULT
from sharestream.core.security import get_current_user
from sharestream.core.branding import site_context
from sharestream.core.templates import render
from sharestream.db.models import SharedTag, SceneViews, VideoOverride
from sharestream.db.session import get_db
from sharestream.schemas.shares import ReorderTagsRequest
from sharestream.services.cache import clear_tag_membership_cache
from sharestream.services.slugs import encode_video_id


logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/__admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    return HTMLResponse(render("admin.html", **site_context(request)))


@router.post("/clear_cache")
async def clear_cache(current_user: str = Depends(get_current_user)):
    # Clear the tag membership cache (e.g. after retagging scenes in Stash so a
    # share immediately reflects the change instead of waiting out the TTL).
    evicted = clear_tag_membership_cache()
    return {"detail": f"Cleared tag membership cache ({evicted} tag(s)).", "evicted": evicted}


@router.get("/admin_settings")
async def admin_settings(current_user: str = Depends(get_current_user)):
    """Operator config the admin UI needs at load time (e.g. the default state of
    the per-share 'Gallery mode?' toggle for new shares)."""
    return {"masonry_default": GALLERY_MASONRY_DEFAULT}


@router.get("/get_video_title/{stash_id}")
async def get_video_title(stash_id: int, current_user: str = Depends(get_current_user)):
    return await fetch_scene_title(stash_id)


@router.get("/shared_videos")
async def shared_videos(current_user: str = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        overrides = db.query(VideoOverride).all()
        logger.info(f"Retrieved {len(overrides)} shared videos (overrides)")

        scene_ids = [int(o.stash_video_id) for o in overrides]
        titles = await get_scene_titles(scene_ids) if scene_ids else {}

        hits_map = {}
        if scene_ids:
            hits_map = {
                int(row.stash_video_id): int(row.views or 0)
                for row in db.query(SceneViews).filter(SceneViews.stash_video_id.in_(scene_ids)).all()
            }

        result = []
        for o in overrides:
            source_title = titles.get(int(o.stash_video_id), f"Scene {o.stash_video_id}")
            display_title = o.custom_title or source_title

            share_url = (
                f"{BASE_DOMAIN}/{o.vanity_slug}"
                if o.vanity_slug
                else f"{BASE_DOMAIN}/v/{encode_video_id(int(o.stash_video_id))}"
            )

            result.append(
                {
                    "share_id": o.vanity_slug,
                    "video_name": display_title,
                    "source_title": source_title,
                    "custom_title": o.custom_title,
                    "stash_video_id": o.stash_video_id,
                    "expires_at": o.expires_at,
                    "hits": hits_map.get(int(o.stash_video_id), 0),
                    "share_url": share_url,
                    "has_password": o.password_hash is not None,
                }
            )

        return result
    except Exception as e:
        logger.error(f"Error listing shared videos: {e}")
        raise HTTPException(status_code=500, detail="Failed to list shared videos")


@router.get("/shared_tags")
async def shared_tags(current_user: str = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        tags = db.query(SharedTag).order_by(SharedTag.sort_order).all()
        logger.info(f"Retrieved {len(tags)} shared tags")
        result = []
        for t in tags:
            share_url = f"{BASE_DOMAIN}/{t.share_id}"
            result.append({
                "share_id": t.share_id,
                "tag_name": t.tag_name,
                "stash_tag_id": t.stash_tag_id,
                "expires_at": t.expires_at,
                "hits": t.hits,
                "share_url": share_url,
                "resolution": t.resolution,
                "has_password": t.password_hash is not None,
                "show_in_gallery": t.show_in_gallery,
                "embed_mode": t.embed_mode,
                "default_sort": t.default_sort,
                "apply_limit_tag": t.apply_limit_tag,
                "gallery_mode": t.gallery_mode,
            })
        return result
    except Exception as e:
        logger.error(f"Error listing shared tags: {e}")
        raise HTTPException(status_code=500, detail="Failed to list shared tags")


@router.put("/reorder_tag_shares")
async def reorder_tag_shares(request: ReorderTagsRequest,
                             current_user: str = Depends(get_current_user),
                             db: Session = Depends(get_db)):
    # Persist a new display order for tag collections (drag-to-reorder in admin).
    try:
        for index, sid in enumerate(request.order):
            tag = db.query(SharedTag).filter(SharedTag.share_id == sid).first()
            if tag:
                tag.sort_order = index + 1  # 1-based so the first tag is never 0
        db.commit()
        logger.info(f"Reordered {len(request.order)} tag shares")
        return {"message": "Order updated"}
    except Exception as e:
        logger.error(f"Error reordering tag shares: {e}")
        raise HTTPException(status_code=500, detail="Failed to reorder tag shares")

@router.get("/lookup_video_titles")
async def lookup_video_titles(
    q: str = Query("", min_length=1),
    current_user: str = Depends(get_current_user),
):
    matches = await search_scene_titles(q)
    logger.info(f"lookup_video_titles q={q!r} returned {len(matches)} match(es)")
    return matches
