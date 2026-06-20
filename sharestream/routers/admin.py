"""Admin panel routes: panel redirect, listings, reordering, cache clear, and
the Stash video-title lookup used by the admin UI. All are JWT-protected except
the static redirect."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from sharestream.backends.stash import fetch_scene_title
from sharestream.config import BASE_DOMAIN
from sharestream.core.security import get_current_user
from sharestream.db.models import SharedTag, SharedVideo
from sharestream.db.session import get_db
from sharestream.schemas.shares import ReorderTagsRequest
from sharestream.services.cache import clear_tag_membership_cache

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/__admin", response_class=RedirectResponse)
async def admin_panel():
    return RedirectResponse(url="/static/admin.html")


@router.post("/clear_cache")
async def clear_cache(current_user: str = Depends(get_current_user)):
    # Clear the tag membership cache (e.g. after retagging scenes in Stash so a
    # share immediately reflects the change instead of waiting out the TTL).
    evicted = clear_tag_membership_cache()
    return {"detail": f"Cleared tag membership cache ({evicted} tag(s)).", "evicted": evicted}


@router.get("/get_video_title/{stash_id}")
async def get_video_title(stash_id: int, current_user: str = Depends(get_current_user)):
    return await fetch_scene_title(stash_id)


@router.get("/shared_videos")
async def shared_videos(current_user: str = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        videos = db.query(SharedVideo).all()
        logger.info(f"Retrieved {len(videos)} shared videos")
        result = []
        for v in videos:
            share_url = f"{BASE_DOMAIN}/{v.share_id}"
            result.append(
                {
                    "share_id": v.share_id,
                    "video_name": f"{v.video_name} ({v.resolution})",
                    "stash_video_id": v.stash_video_id,
                    "expires_at": v.expires_at,
                    "hits": v.hits,
                    "share_url": share_url,
                    "resolution": v.resolution,
                    "has_password": v.password_hash is not None,
                    "show_in_gallery": v.show_in_gallery,
                    "embed_mode": v.embed_mode,
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
