"""Individual video shares: create/edit/delete (admin), the public share page,
and password verification."""
from __future__ import annotations

import datetime
import logging
from datetime import timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from sharestream.backends.stash import get_video_details
from sharestream.config import BASE_DOMAIN, SHARES_DIR
from sharestream.core.branding import site_context
from sharestream.core.security import get_current_user, pwd_context
from sharestream.core.templates import render
from sharestream.db.models import SharedTag, SharedVideo
from sharestream.db.session import get_db
from sharestream.schemas.shares import ShareVideoRequest
from sharestream.services import access
from sharestream.services.embed_policy import normalize_embed_mode, should_embed_full
from sharestream.services.hits import get_total_plays, increment_share_hit
from sharestream.services.media_proxy import generate_m3u8_file
from sharestream.services.slugs import generate_share_id, validate_custom_share_id
from sharestream.services.visitors import log_first_visit

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/share")
async def share_video(request: ShareVideoRequest,
                      current_user: str = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    expires_at = datetime.datetime.now(timezone.utc) + datetime.timedelta(days=request.days_valid)
    try:
        if request.custom_share_id:
            share_id = validate_custom_share_id(request.custom_share_id, db)
        else:
            share_id = generate_share_id()

        password_hash = None
        if request.password:
            password_hash = pwd_context.hash(request.password)

        shared_video = SharedVideo(
            share_id=share_id,
            video_name=request.video_name,
            stash_video_id=request.stash_video_id,
            expires_at=expires_at,
            hits=0,
            resolution=request.resolution,
            password_hash=password_hash,
            show_in_gallery=request.show_in_gallery if hasattr(request, 'show_in_gallery') else False,
            embed_mode=normalize_embed_mode(request.embed_mode)
        )
        db.add(shared_video)
        db.commit()

        # Generate static .m3u8 file
        if not await generate_m3u8_file(share_id, request.stash_video_id, request.resolution):
            raise HTTPException(status_code=500, detail="Failed to generate .m3u8 file")

        logger.info(f"Video shared: share_id={share_id}, stash_video_id={request.stash_video_id}, resolution={request.resolution}")
        share_url = f"{BASE_DOMAIN}/{share_id}"
        if request.password:
            share_url += f"?pwd={request.password}"
        return {"share_url": share_url}
    except HTTPException:
        # Surface validation errors (e.g. reserved/duplicate custom slug) as-is.
        raise
    except Exception as e:
        logger.error(f"Error sharing video: {e}")
        raise HTTPException(status_code=500, detail="Failed to share video")


@router.put("/edit_share/{share_id}")
async def edit_share(share_id: str, request: ShareVideoRequest,
                     current_user: str = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    try:
        video = db.query(SharedVideo).filter(SharedVideo.share_id == share_id).first()
        if not video:
            raise HTTPException(status_code=404, detail="Share link not found")
        video.video_name = request.video_name
        video.expires_at = datetime.datetime.now(timezone.utc) + datetime.timedelta(days=request.days_valid)
        video.resolution = request.resolution
        # Password: explicit clear wins; a new value sets it; blank keeps existing.
        if request.clear_password:
            video.password_hash = None
        elif request.password:
            video.password_hash = pwd_context.hash(request.password)
        video.show_in_gallery = request.show_in_gallery if hasattr(request, 'show_in_gallery') else False
        # Only touch embed_mode if the client actually sent it, so editing via
        # the (embed-less) modal doesn't silently wipe an existing override.
        if 'embed_mode' in request.model_fields_set:
            video.embed_mode = normalize_embed_mode(request.embed_mode)
        db.commit()

        # Regenerate .m3u8 file
        if not await generate_m3u8_file(share_id, request.stash_video_id, request.resolution):
            raise HTTPException(status_code=500, detail="Failed to regenerate .m3u8 file")

        logger.info(f"Share updated: share_id={share_id}")
        return {"message": "Share updated"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating share: {e}")
        raise HTTPException(status_code=500, detail="Failed to update share")


@router.delete("/delete_share/{share_id}")
async def delete_share(share_id: str,
                       current_user: str = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    try:
        video = db.query(SharedVideo).filter(SharedVideo.share_id == share_id).first()
        if not video:
            raise HTTPException(status_code=404, detail="Share link not found")
        db.delete(video)
        db.commit()

        # Delete .m3u8 file
        m3u8_path = SHARES_DIR / f"{share_id}.m3u8"
        if m3u8_path.exists():
            m3u8_path.unlink()
            logger.info(f"Deleted .m3u8 file for share_id={share_id}")

        logger.info(f"Share deleted: share_id={share_id}")
        return {"message": "Share deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting share: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete share")


@router.get("/share/{share_id}", response_class=HTMLResponse, response_model=None)
async def share_page(share_id: str, request: Request = None, db: Session = Depends(get_db)):
    video = db.query(SharedVideo).filter_by(share_id=share_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Share link not found")

    log_first_visit(request, share_id, kind="share")

    access.ensure_not_expired(video.expires_at, "Share link has expired")

    # password gate
    locked = access.password_prompt_if_locked(request, share_id, video.password_hash, video.video_name)
    if locked is not None:
        return locked

    # count hit BEFORE showing page, then show the aggregate play count for this
    # video across every share context (not just this individual share's tally).
    increment_share_hit(db, video)
    hit_count = get_total_plays(db, video.stash_video_id)

    # Get full video details from Stash
    video_details = await get_video_details(video.stash_video_id)

    # Decide whether the og:video embed should be the full video or the short
    # preview clip (per-share override, else config default).
    _files = (video_details or {}).get("files") or []
    _size = _files[0].get("size") if _files else None
    _duration = (video_details or {}).get("duration")
    if should_embed_full(video.embed_mode, _duration, _size):
        embed_video_url = f"{BASE_DOMAIN}/{share_id}/full.mp4"
    else:
        embed_video_url = f"{BASE_DOMAIN}/share/{share_id}/stream.mp4"

    context = site_context()
    context.update(
        video_name=video.video_name,
        share_id=share_id,
        video_details=video_details,
        embed_video_url=embed_video_url,
        hit_count=hit_count,
    )
    return HTMLResponse(render("video-player.html", **context))


@router.post("/share/{share_id}/verify")
async def verify_password(share_id: str, password: str = Form(...),
                          next_url: str = Form(None, alias="next"),
                          db: Session = Depends(get_db)):
    """Verify a password for an individual video OR a tag share. On success we
    set a signed, share-scoped cookie (never a URL flag) and redirect to the
    page the viewer originally asked for (validated same-origin ``next``), or
    the canonical short URL as a fallback."""
    safe_next = access.safe_next_path(next_url)
    vid = db.query(SharedVideo).filter_by(share_id=share_id).first()
    tag = None if vid else db.query(SharedTag).filter_by(share_id=share_id).first()
    target = vid or tag
    display_name = vid.video_name if vid else (f"Tag: {tag.tag_name}" if tag else "")
    if not target or not target.password_hash \
       or not pwd_context.verify(password, target.password_hash):
        html = render(
            "password-prompt.html",
            **site_context(),
            video_name=display_name,
            share_id=share_id,
            error_message="Incorrect password. Please try again.",
            # Preserve the intended destination across a failed attempt.
            next_url=safe_next or "",
        )
        return HTMLResponse(html, status_code=401)
    # success → set a signed cookie and 303 back to the requested page
    resp = RedirectResponse(safe_next or f"/{share_id}", status_code=303)
    access.set_unlock_cookie(resp, share_id)
    return resp
