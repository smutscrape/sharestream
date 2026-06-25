"""Canonical video route: ``/v/{slug}``.

A video is reached at exactly one canonical URL. ``{slug}`` is either the Sqids
encoding of the Stash scene id (default) or a ``VideoOverride.vanity_slug``
(custom alias). When a scene has a vanity slug, the Sqids form 301s to it so the
vanity URL is the single canonical one. Access is resolved by the centralized
gate (``resolve_scene_access``: hidden→404, password→prompt, else allow).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from sharestream.backends.stash import get_video_details
from sharestream.config import BASE_DOMAIN
from sharestream.core.branding import site_context
from sharestream.core.security import pwd_context
from sharestream.core.templates import render
from sharestream.db.models import VideoOverride
from sharestream.db.session import get_db
from sharestream.services import access
from sharestream.services.embed_policy import should_embed_full
from sharestream.services.hits import get_total_plays
from sharestream.services.slugs import decode_video_id, encode_video_id
from sharestream.services.visitors import log_first_visit

logger = logging.getLogger(__name__)

router = APIRouter()


def _resolve_slug(db: Session, slug: str) -> tuple[int, VideoOverride | None] | None:
    """Resolve a /v/ slug to (stash_video_id, override). Tries the Sqids decode
    first, then a vanity-slug lookup. Returns None if neither matches."""
    sid = decode_video_id(slug)
    if sid is not None:
        override = db.query(VideoOverride).filter(VideoOverride.stash_video_id == sid).first()
        return sid, override
    override = db.query(VideoOverride).filter(VideoOverride.vanity_slug == slug).first()
    if override is not None:
        return int(override.stash_video_id), override
    return None


def _canonical_slug(stash_video_id: int, override: VideoOverride | None) -> str:
    """The single canonical slug for a scene: its vanity slug if set, else the
    Sqids encoding."""
    if override is not None and override.vanity_slug:
        return override.vanity_slug
    return encode_video_id(stash_video_id)


@router.get("/v/{slug}", response_class=HTMLResponse, response_model=None)
async def video_page(slug: str, request: Request = None, db: Session = Depends(get_db)):
    resolved = _resolve_slug(db, slug)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Video not found")
    stash_video_id, override = resolved

    # Canonicalize: a scene with a vanity slug has exactly one canonical URL (the
    # vanity one); the Sqids form 301s to it.
    canonical = _canonical_slug(stash_video_id, override)
    if slug != canonical:
        return RedirectResponse(url=f"/v/{canonical}", status_code=301)

    log_first_visit(request, f"v-{stash_video_id}", kind="video")

    decision = await access.resolve_scene_access(request, db, stash_video_id)
    if decision == access.ACCESS_NOT_FOUND:
        raise HTTPException(status_code=404, detail="Video not found")
    if decision == access.ACCESS_PASSWORD_REQUIRED:
        # Password prompt keyed to the scene id; on unlock, return here. The form
        # posts to this slug's verify endpoint.
        return access.password_prompt_if_locked(
            request, str(stash_video_id), override.password_hash if override else None,
            "Video", verify_action=f"/v/{slug}/verify",
        )

    video_details = await get_video_details(stash_video_id) or {}

    # Play-count increment is owned by Phase 3; Phase 1 only displays the
    # aggregate. get_total_plays still reads the legacy counters until Phase 3
    # repoints it to scene_views.
    hit_count = get_total_plays(db, stash_video_id)

    # Decide og:video embed (full video vs short preview) per the scene's
    # override embed_mode (none yet on VideoOverride → config default).
    _files = (video_details or {}).get("files") or []
    _size = _files[0].get("size") if _files else None
    _duration = (video_details or {}).get("duration")
    media_base = f"/media/{stash_video_id}"
    if should_embed_full(None, _duration, _size):
        embed_video_url = f"{BASE_DOMAIN}{media_base}/full.mp4"
    else:
        embed_video_url = f"{BASE_DOMAIN}{media_base}/stream.mp4"

    canonical_url = f"{BASE_DOMAIN}/v/{canonical}"
    context = site_context(request)
    context.update(
        video_name=video_details.get("title") or "Video",
        stash_video_id=stash_video_id,
        video_details=video_details,
        embed_video_url=embed_video_url,
        hit_count=hit_count,
        canonical_url=canonical_url,
    )
    return HTMLResponse(render("video-player.html", **context))


@router.post("/v/{slug}/verify")
async def verify_video_password(slug: str, password: str = Form(...),
                                next_url: str = Form(None, alias="next"),
                                db: Session = Depends(get_db)):
    """Verify a password for a VideoOverride-gated scene. On success, set the
    scene-keyed unlock cookie and 303 back to the requested page (or /v/{canonical})."""
    resolved = _resolve_slug(db, slug)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Video not found")
    stash_video_id, override = resolved
    canonical = _canonical_slug(stash_video_id, override)
    safe_next = access.safe_next_path(next_url)
    password_hash = override.password_hash if override else None
    if not password_hash or not pwd_context.verify(password, password_hash):
        html = render(
            "password-prompt.html",
            **site_context(),
            video_name="Video",
            share_id=str(stash_video_id),
            error_message="Incorrect password. Please try again.",
            next_url=safe_next or "",
        )
        return HTMLResponse(html, status_code=401)
    resp = RedirectResponse(safe_next or f"/v/{canonical}", status_code=303)
    access.set_unlock_cookie(resp, str(stash_video_id))
    return resp
