"""Individual video shares: create/edit/delete (admin), the public share page,
and password verification."""
from __future__ import annotations

import datetime
import logging
from datetime import timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from sharestream.config import BASE_DOMAIN, DEFAULT_RESOLUTION, SHARES_DIR
from sharestream.core.branding import site_context
from sharestream.core.security import get_current_user, pwd_context
from sharestream.core.templates import render
from sharestream.db.models import SharedTag, VideoOverride
from sharestream.db.session import get_db
from sharestream.services.slugs import RESERVED_SLUGS, canonical_video_slug, decode_video_id
from sharestream.schemas.shares import ShareVideoRequest
from sharestream.services import access
from sharestream.services.embed_policy import normalize_embed_mode
from sharestream.services.media_proxy import generate_m3u8_file
from sharestream.services.slugs import generate_share_id, validate_custom_share_id, encode_video_id, decode_video_id

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/share")
async def share_video(request: ShareVideoRequest,
                      current_user: str = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    """Create a share for a scene.

    - Custom slug OR password OR title override → creates/updates a VideoOverride
      and returns the individual-share URL ``/{slug}``.
    - Otherwise → returns the stateless ``/v/{sqid}`` URL; no DB row is created.
    """
    expires_at = datetime.datetime.now(timezone.utc) + datetime.timedelta(days=request.days_valid)
    title_override = (request.video_name or "").strip() or None

    try:
        has_capability = bool(request.custom_share_id) or bool(request.password) or bool(title_override)

        if has_capability:
            override = db.query(VideoOverride).filter(
                VideoOverride.stash_video_id == request.stash_video_id
            ).first()

            if request.custom_share_id:
                vanity_slug = validate_custom_share_id(request.custom_share_id, db)
            elif override and override.vanity_slug:
                vanity_slug = override.vanity_slug
            else:
                vanity_slug = _mint_unique_slug(db)

            password_hash = pwd_context.hash(request.password) if request.password else None

            if override is not None:
                override.vanity_slug = vanity_slug
                override.password_hash = password_hash
                override.expires_at = expires_at
                override.custom_title = title_override
            else:
                override = VideoOverride(
                    stash_video_id=request.stash_video_id,
                    vanity_slug=vanity_slug,
                    password_hash=password_hash,
                    expires_at=expires_at,
                    custom_title=title_override,
                )
                db.add(override)

            db.commit()

            logger.info(
                f"Video share (override): vanity_slug={vanity_slug}, "
                f"stash_video_id={request.stash_video_id}, "
                f"has_password={bool(password_hash)}, "
                f"has_custom_title={bool(title_override)}"
            )

            share_url = f"{BASE_DOMAIN}/{vanity_slug}"
            if request.password:
                share_url += f"?pwd={request.password}"

            return {"share_url": share_url}

        sqid = encode_video_id(request.stash_video_id)
        logger.info(f"Video share (stateless): stash_video_id={request.stash_video_id}")
        return {"share_url": f"{BASE_DOMAIN}/v/{sqid}"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sharing video: {e}")
        raise HTTPException(status_code=500, detail="Failed to share video")


def _mint_unique_slug(db: Session, length: int = 10) -> str:
    """Generate a random, unique vanity slug that passes reserved-word checks."""
    import secrets
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    for _ in range(100):
        slug = "u" + "".join(secrets.choice(alphabet) for _ in range(length - 1))
        if slug in RESERVED_SLUGS:
            continue
        if db.query(VideoOverride).filter(VideoOverride.vanity_slug == slug).first() or \
           db.query(SharedTag).filter(SharedTag.share_id == slug).first():
            continue
        return slug
    raise HTTPException(status_code=500, detail="Failed to generate a unique share slug")


@router.put("/edit_share/{share_id}")
async def edit_share(share_id: str, request: ShareVideoRequest,
                     current_user: str = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    try:
        override = db.query(VideoOverride).filter(VideoOverride.vanity_slug == share_id).first()
        if not override:
            raise HTTPException(status_code=404, detail="Share link not found")

        title_override = (request.video_name or "").strip() or None

        override.expires_at = datetime.datetime.now(timezone.utc) + datetime.timedelta(days=request.days_valid)
        override.custom_title = title_override

        if request.clear_password:
            override.password_hash = None
        elif request.password:
            override.password_hash = pwd_context.hash(request.password)

        db.commit()

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
        override = db.query(VideoOverride).filter(
            VideoOverride.vanity_slug == share_id
        ).first()

        if not override:
            sid = decode_video_id(share_id)
            if sid is not None:
                override = db.query(VideoOverride).filter(
                    VideoOverride.stash_video_id == sid
                ).first()

        if not override:
            raise HTTPException(status_code=404, detail="Share link not found")

        vanity_slug = override.vanity_slug

        db.delete(override)
        db.commit()

        if vanity_slug:
            for stale in SHARES_DIR.glob(f"slug-{vanity_slug}-*.m3u8"):
                try:
                    stale.unlink()
                except OSError:
                    pass

        logger.info(f"Share deleted: share_id={share_id}")
        return {"message": "Share deleted"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting share: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete share")


@router.get("/share/{share_id}", response_class=HTMLResponse, response_model=None)
async def share_page(share_id: str, request: Request = None, db: Session = Depends(get_db)):
    """Legacy individual-share URL → 301 to the canonical /v/{slug}, carrying the
    unlock cookie so a viewer who unlocked the old link stays unlocked."""
    override = db.query(VideoOverride).filter(VideoOverride.vanity_slug == share_id).first()
    if not override:
        raise HTTPException(status_code=404, detail="Share link not found")
        
    slug = canonical_video_slug(db, override.stash_video_id)
    resp = RedirectResponse(url=f"/v/{slug}", status_code=301)
    access.carry_unlock_cookie(request, resp, share_id, override.stash_video_id)
    return resp


@router.post("/share/{share_id}/verify")
async def verify_password(share_id: str, password: str = Form(...),
                          next_url: str = Form(None, alias="next"),
                          db: Session = Depends(get_db)):
    """Verify a password for an individual video OR a tag share. On success we
    set a signed, share-scoped cookie (never a URL flag) and redirect to the
    page the viewer originally asked for (validated same-origin ``next``), or
    the canonical short URL as a fallback."""
    safe_next = access.safe_next_path(next_url)
    
    # Try VideoOverride by vanity_slug
    override = db.query(VideoOverride).filter(VideoOverride.vanity_slug == share_id).first()
    if override:
        if not override.password_hash:
            resp = RedirectResponse(safe_next or f"/{share_id}", status_code=303)
            access.set_unlock_cookie(resp, str(override.stash_video_id))
            return resp
        if pwd_context.verify(password, override.password_hash):
            resp = RedirectResponse(safe_next or f"/{share_id}", status_code=303)
            access.set_unlock_cookie(resp, str(override.stash_video_id))
            return resp
        html = render(
            "password-prompt.html",
            **site_context(),
            video_name="Video",
            share_id=share_id,
            error_message="Incorrect password. Please try again.",
            next_url=safe_next or "",
        )
        return HTMLResponse(html, status_code=401)


    # Fallback to SharedTag
    tag = db.query(SharedTag).filter_by(share_id=share_id).first()
    display_name = f"Tag: {tag.tag_name}" if tag else ""
    if not tag or not tag.password_hash \
       or not pwd_context.verify(password, tag.password_hash):
        html = render(
            "password-prompt.html",
            **site_context(),
            video_name=display_name,
            share_id=share_id,
            error_message="Incorrect password. Please try again.",
            next_url=safe_next or "",
        )
        return HTMLResponse(html, status_code=401)
        
    resp = RedirectResponse(safe_next or f"/{share_id}", status_code=303)
    access.set_unlock_cookie(resp, share_id)
    return resp
