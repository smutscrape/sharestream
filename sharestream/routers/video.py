"""Video routes: the global ``/v/{slug}`` and the individual-share ``/{slug}``.

**Context-aware routing (no redirects between the two):**

* ``/v/{slug}`` — global / stateless URL (used by Homepage and Tag Galleries).
  Stash tag visibility governs: ``HIDDEN`` -> 404, ``PUBLIC``/``LISTED`` -> render,
  unlisted -> 404. A ``VideoOverride.password_hash`` is intentionally *not*
  checked here so a PUBLIC video plays freely in galleries without prompting.

* ``/{slug}`` — individual share URL (used when an admin generates a share
  with a custom slug and/or password). ``VideoOverride.password_hash`` governs
  access; stash tag visibility is ignored (an unlisted scene is still reachable
  via its share slug, since the slug itself is the capability).

Both routes render the ``video-player.html`` template directly at their own URL.
When a scene carries a ``VideoOverride.vanity_slug``, the /v/{sqid} form 301s
to /v/{vanity_slug} so there is exactly one canonical global URL.
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
    """The single canonical global slug for a scene: its vanity slug if set, else
    the Sqids encoding."""
    if override is not None and override.vanity_slug:
        return override.vanity_slug
    return encode_video_id(stash_video_id)


# ---------------------------------------------------------------------------
# Shared renderer for the video player template.
# ---------------------------------------------------------------------------
async def _render_video_page(request, db, stash_video_id, override, slug_for_context,
                              verify_action_prefix="/v"):
    """Build the ``video-player.html`` response for a scene cleared for playback.

    The template receives the Hashid slug (``hashid`` == vanity slug or Sqids
    encoding) — never the raw ``stash_video_id`` — so generated meta/embed/asset
    URLs are non-sequential. ``verify_action_prefix`` lets the /{slug} override
    route point the password form at its own endpoint (``/{slug}/verify``) instead
    of ``/v/``."""
    video_details = await get_video_details(stash_video_id) or {}
    hit_count = get_total_plays(db, stash_video_id)
    hashid = slug_for_context

    _files = (video_details or {}).get("files") or []
    _size = _files[0].get("size") if _files else None
    _duration = (video_details or {}).get("duration")
    media_base = f"/media/{hashid}"
    if should_embed_full(None, _duration, _size):
        embed_video_url = f"{BASE_DOMAIN}{media_base}/full.mp4"
    else:
        embed_video_url = f"{BASE_DOMAIN}{media_base}/stream.mp4"

    canonical_url = f"{BASE_DOMAIN}/v/{slug_for_context if verify_action_prefix == '/v' else hashid}"
    context = site_context(request)
    context.update(
        video_name=video_details.get("title") or "Video",
        hashid=hashid,
        video_details=video_details,
        embed_video_url=embed_video_url,
        hit_count=hit_count,
        canonical_url=canonical_url,
        verify_action=f"{verify_action_prefix}/{slug_for_context}/verify" if verify_action_prefix else f"/{slug_for_context}/verify",
    )
    return HTMLResponse(render("video-player.html", **context))


# ---------------------------------------------------------------------------
# Global / stateless URL: /v/{slug}
# ---------------------------------------------------------------------------
@router.get("/v/{slug}", response_class=HTMLResponse, response_model=None)
async def video_page(slug: str, request: Request = None, db: Session = Depends(get_db)):
    resolved = _resolve_slug(db, slug)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Video not found")
    stash_video_id, override = resolved

    # Canonicalize: when a vanity slug is set, the /v/{sqid} form 301s so that
    # only the vanity URL is reachable globally.
    canonical = _canonical_slug(stash_video_id, override)
    if slug != canonical:
        return RedirectResponse(url=f"/v/{canonical}", status_code=301)

    log_first_visit(request, f"v-{stash_video_id}", kind="video")

    # Origin="global": stash-tag visibility governs; password_hash is ignored so
    # a PUBLIC video can be embedded/presented in galleries without prompting.
    decision = await access.resolve_scene_access(request, db, stash_video_id, origin="global")
    if decision == access.ACCESS_NOT_FOUND:
        raise HTTPException(status_code=404, detail="Video not found")
    # The template needs the Hashid (Sqids of stash_video_id) for media URLs —
    # NOT the canonical slug, which may be an arbitrary vanity slug that doesn't
    # decode via decode_video_id.
    from sharestream.services.slugs import encode_video_id
    sqid = encode_video_id(stash_video_id)

    if decision == access.ACCESS_PASSWORD_REQUIRED:
        return access.password_prompt_if_locked(
            request, str(stash_video_id), override.password_hash if override else None,
            "Video", verify_action=f"/v/{slug}/verify",
        )

    return await _render_video_page(request, db, stash_video_id, override, sqid,
                                    verify_action_prefix="/v")


# ---------------------------------------------------------------------------
# Password verification endpoints
# ---------------------------------------------------------------------------
@router.post("/v/{slug}/verify")
async def verify_video_password(slug: str, password: str = Form(...),
                                next_url: str = Form(None, alias="next"),
                                db: Session = Depends(get_db)):
    """Verify a password for the global /v/ scene route. On success, set the
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


