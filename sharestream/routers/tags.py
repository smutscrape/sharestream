"""Tag shares: lookup/create/edit/delete (admin), the tag gallery page, and the
individual-video-within-a-tag page."""
from __future__ import annotations

import datetime
import logging
from datetime import timezone
from urllib.parse import unquote_plus

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from sharestream.backends.stash import get_video_details, get_videos_by_tag, get_videos_by_tag_name
from sharestream.config import BASE_DOMAIN, DEFAULT_SORT, SHARES_DIR
from sharestream.core.branding import site_context
from sharestream.core.security import get_current_user, pwd_context
from sharestream.core.templates import render
from sharestream.db.models import SharedTag
from sharestream.db.session import get_db
from sharestream.schemas.shares import ShareTagRequest
from sharestream.services import access
from sharestream.services.cache import is_video_in_tag
from sharestream.services.embed_policy import normalize_embed_mode, should_embed_full
from sharestream.services.galleries import build_tag_gallery_context, normalize_sort
from sharestream.services.hits import get_total_plays, increment_tag_hit, increment_tag_video_hit
from sharestream.services.slugs import generate_share_id, validate_custom_share_id
from sharestream.services.visitors import log_first_visit

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/lookup_tag/{tag_name}")
async def lookup_tag(tag_name: str, current_user: str = Depends(get_current_user)):
    """Lookup a tag and return info about it"""
    tag_name = unquote_plus(tag_name)
    try:
        # Admin lookup shows full tag membership; limit_to_tag applies at display time.
        videos, tag_info = await get_videos_by_tag_name(tag_name, respect_limit_tag=False)

        if not tag_info:
            raise HTTPException(status_code=404, detail=f"Tag '{tag_name}' not found")

        logger.info(f"Tag lookup successful: {tag_name} -> {tag_info['id']} ({len(videos)} videos)")
        return {
            "tag_info": tag_info,
            "video_count": len(videos)
        }
    except HTTPException:
        # Re-raise HTTPException as-is (don't convert to 500 error)
        raise
    except Exception as e:
        logger.error(f"Error looking up tag '{tag_name}': {e}")
        raise HTTPException(status_code=500, detail="Failed to lookup tag")


@router.post("/share_tag")
async def share_tag(request: ShareTagRequest,
                    current_user: str = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    """Share all videos with a specific tag"""
    logger.info(f"Tag share request: tag_name={request.tag_name}, tag_id={request.tag_id}")

    # Verify the tag exists and has videos using the provided tag_id
    try:
        videos, _ = await get_videos_by_tag(request.tag_id, respect_limit_tag=False)

        if not videos:
            logger.warning(f"No videos found for tag ID {request.tag_id}")
            raise HTTPException(status_code=404, detail=f"No videos found for tag ID '{request.tag_id}'")
    except HTTPException:
        # Re-raise HTTPException as-is
        raise
    except Exception as e:
        logger.error(f"Error getting videos for tag {request.tag_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to verify tag")

    # Generate share ID based on request
    if request.custom_share_id:
        share_id = validate_custom_share_id(request.custom_share_id, db)
    else:
        share_id = generate_share_id()

    expires_at = datetime.datetime.now(timezone.utc) + datetime.timedelta(days=request.days_valid)

    try:
        password_hash = None
        if request.password:
            password_hash = pwd_context.hash(request.password)

        # Append new tag shares to the end of the display order.
        max_order = db.query(func.max(SharedTag.sort_order)).scalar() or 0

        shared_tag = SharedTag(
            share_id=share_id,
            tag_name=request.tag_name,
            stash_tag_id=request.tag_id,
            expires_at=expires_at,
            hits=0,
            resolution=request.resolution,
            password_hash=password_hash,
            show_in_gallery=request.show_in_gallery,
            embed_mode=normalize_embed_mode(request.embed_mode),
            default_sort=normalize_sort(request.default_sort),
            apply_limit_tag=request.apply_limit_tag,
            sort_order=max_order + 1
        )
        db.add(shared_tag)
        db.commit()

        logger.info(f"Tag shared: share_id={share_id}, tag_name={request.tag_name}, tag_id={request.tag_id}, video_count={len(videos)}")
        share_url = f"{BASE_DOMAIN}/{share_id}"
        if request.password:
            share_url += f"?pwd={request.password}"

        return {
            "share_url": share_url,
            "tag_name": request.tag_name,
            "video_count": len(videos),
            "share_id": share_id
        }
    except HTTPException:
        # Re-raise HTTPException as-is
        raise
    except Exception as e:
        logger.error(f"Error sharing tag: {e}")
        raise HTTPException(status_code=500, detail="Failed to share tag")


@router.put("/edit_tag_share/{share_id}")
async def edit_tag_share(share_id: str, request: ShareTagRequest,
                         current_user: str = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    try:
        tag = db.query(SharedTag).filter(SharedTag.share_id == share_id).first()
        if not tag:
            raise HTTPException(status_code=404, detail="Tag share not found")

        tag.tag_name = request.tag_name
        tag.stash_tag_id = request.tag_id
        tag.expires_at = datetime.datetime.now(timezone.utc) + datetime.timedelta(days=request.days_valid)
        tag.resolution = request.resolution
        # Password: explicit clear wins; a new value sets it; blank keeps existing.
        if request.clear_password:
            tag.password_hash = None
        elif request.password:
            tag.password_hash = pwd_context.hash(request.password)
        tag.show_in_gallery = request.show_in_gallery
        if 'embed_mode' in request.model_fields_set:
            tag.embed_mode = normalize_embed_mode(request.embed_mode)
        if 'default_sort' in request.model_fields_set:
            tag.default_sort = normalize_sort(request.default_sort)
        if 'apply_limit_tag' in request.model_fields_set:
            tag.apply_limit_tag = request.apply_limit_tag
        db.commit()

        # Drop cached per-video playlists for this tag so any resolution change
        # takes effect (they're regenerated on demand at the new resolution).
        for stale in SHARES_DIR.glob(f"tag-{share_id}-video-*.m3u8"):
            try:
                stale.unlink()
            except OSError:
                pass

        logger.info(f"Tag share updated: share_id={share_id}")
        return {"message": "Tag share updated"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating tag share: {e}")
        raise HTTPException(status_code=500, detail="Failed to update tag share")


@router.delete("/delete_tag_share/{share_id}")
async def delete_tag_share(share_id: str,
                           current_user: str = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    try:
        tag = db.query(SharedTag).filter(SharedTag.share_id == share_id).first()
        if not tag:
            raise HTTPException(status_code=404, detail="Tag share not found")

        db.delete(tag)
        db.commit()

        logger.info(f"Tag share deleted: share_id={share_id}")
        return {"message": "Tag share deleted"}
    except HTTPException:
        # Re-raise HTTPException as-is
        raise
    except Exception as e:
        logger.error(f"Error deleting tag share: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete tag share")


@router.get("/tag/{share_id}", response_class=HTMLResponse, response_model=None)
async def tag_share_page(share_id: str, request: Request = None, page: int = 1, sort: str | None = None,
                         db: Session = Depends(get_db)):
    tag_share = db.query(SharedTag).filter_by(share_id=share_id).first()
    if not tag_share:
        raise HTTPException(status_code=404, detail="Tag share not found")

    log_first_visit(request, share_id, kind="tag")

    access.ensure_not_expired(tag_share.expires_at, "Tag share has expired")

    # password gate
    locked = access.password_prompt_if_locked(request, share_id, tag_share.password_hash,
                                               f"Tag: {tag_share.tag_name}")
    if locked is not None:
        return locked

    # count hit BEFORE showing page
    increment_tag_hit(db, tag_share)

    # An explicit ?sort= (from the dropdown) wins; otherwise use this share's
    # configured default sort, falling back to the global config default.
    effective_sort = normalize_sort(sort) or normalize_sort(tag_share.default_sort) or DEFAULT_SORT

    context = await build_tag_gallery_context(db, tag_share, share_id, page=page, sort=effective_sort,
                                             request=request)
    return HTMLResponse(render("gallery.html", **context))


@router.get("/tag/{share_id}/video/{video_id}", response_class=HTMLResponse, response_model=None)
async def tag_video_page(share_id: str, video_id: int, request: Request = None,
                         db: Session = Depends(get_db)):
    tag_share = db.query(SharedTag).filter_by(share_id=share_id).first()
    if not tag_share:
        raise HTTPException(status_code=404, detail="Tag share not found")

    access.ensure_not_expired(tag_share.expires_at, "Tag share has expired")

    # password gate — a tag's password must protect its individual video pages
    # too, not just the gallery. Keyed to the tag share id so unlocking the
    # gallery (or any one video) unlocks the whole tag for this browser.
    locked = access.password_prompt_if_locked(request, share_id, tag_share.password_hash,
                                               f"Tag: {tag_share.tag_name}")
    if locked is not None:
        return locked

    # Verify membership via the cached tag->video-id set instead of pulling the
    # whole tag's scene list just to locate one video. Only a public, home-
    # featured tag share stays limited to limit_to_tag; a password-protected OR
    # non-featured (capability-URL) share reaches the tag's full contents.
    respect_limit = access.tag_share_respects_limit_tag(tag_share.password_hash,
                                                        tag_share.show_in_gallery,
                                                        tag_share.apply_limit_tag)
    if not await is_video_in_tag(tag_share.stash_tag_id, video_id,
                                 respect_limit_tag=respect_limit):
        raise HTTPException(status_code=404, detail="Video not found in this tag")

    # Track hits for this video (per tag-share), then display the aggregate play
    # count for the underlying video across every share context.
    increment_tag_video_hit(db, share_id, video_id)
    total_plays = get_total_plays(db, video_id)

    # Fetch the full metadata so a tag video page shows exactly the same detail
    # as an individually-shared video. Falls back to an empty dict if it fails.
    video_details = await get_video_details(video_id) or {}

    # Decide og:video embed (full vs preview). Tag shares carry their own
    # embed_mode override which applies to every video opened within them.
    _files = (video_details or {}).get("files") or []
    _size = _files[0].get("size") if _files else None
    _duration = (video_details or {}).get("duration")
    composite_id = f"tag-{share_id}-video-{video_id}"
    if should_embed_full(tag_share.embed_mode, _duration, _size):
        embed_video_url = f"{BASE_DOMAIN}/{composite_id}/full.mp4"
    else:
        embed_video_url = f"{BASE_DOMAIN}/tag/{share_id}/video/{video_id}/stream.mp4"

    context = site_context(request)
    context.update(
        video_name=video_details.get("title") or "Video",
        share_id=composite_id,  # This maps to the m3u8 URL
        video_details=video_details,
        embed_video_url=embed_video_url,
        hit_count=total_plays,
    )
    return HTMLResponse(render("video-player.html", **context))
