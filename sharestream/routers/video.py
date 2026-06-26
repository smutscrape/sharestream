"""Video routes: the global ``/v/{slug}`` and the individual-share ``/{slug}``.

**Context-aware routing (no redirects between the two):**

* ``/v/{slug}`` — global / stateless URL (used by Homepage and Tag Galleries).
  Stash tag visibility governs: ``HIDDEN`` -> 404, ``PUBLIC``/``LISTED`` -> render,
  unlisted -> 404. A ``VideoOverride.password_hash`` is intentionally *not*
  checked here so a PUBLIC video plays freely in galleries without prompting.
  Both ``/v/{sqid}`` and ``/v/{vanity_slug}`` render directly — no 301
  canonicalisation redirect within /v/.

* ``/{slug}`` — individual share URL (used when an admin generates a share
  with a custom slug and/or password). ``VideoOverride.password_hash`` governs
  access; stash tag visibility is ignored (an unlisted scene is still reachable
  via its share slug, since the slug itself is the capability).

Both routes render the ``video-player.html`` template directly at their own URL.
The ``<link rel="canonical">`` and ``og:url`` always match the URL the viewer
requested (no rewriting).  Media URLs always use the Sqids hashid, never the
vanity slug or raw stash id, so ``/media/{sqid}/...`` is consistent regardless
of which ``/v/`` form was used.
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


# ---------------------------------------------------------------------------
# Shared renderer for the video player template.
# ---------------------------------------------------------------------------
async def _render_video_page(request, db, stash_video_id, override, slug_for_context,
                              verify_action_prefix="/v", page_slug=None,
                              via_share_id=None):
    """Build the ``video-player.html`` response for a scene cleared for playback.

    The template receives the Hashid slug (``hashid`` == vanity slug or Sqids
    encoding) — never the raw ``stash_video_id`` — so generated meta/embed/asset
    URLs are non-sequential. ``verify_action_prefix`` lets the /{slug} override
    route point the password form at its own endpoint (``/{slug}/verify``) instead
    of ``/v/``.  ``page_slug`` is the slug that appears in the browser address bar;
    it determines the canonical / og:url.  When omitted the canonical URL is
    derived from ``slug_for_context`` and ``verify_action_prefix``.

    ``via_share_id`` (optional) is the gallery share id when this video is being
    rendered inside a curated Gallery (``/{gallery_slug}/{sqid}``); it is passed
    to the template so media URLs can append ``?via=<share_id>`` and the password
    prompt can target the gallery-scoped route."""
    video_details = await get_video_details(stash_video_id) or {}
    hit_count = get_total_plays(db, stash_video_id)
    hashid = slug_for_context

    _files = (video_details or {}).get("files") or []
    _size = _files[0].get("size") if _files else None
    _duration = (video_details or {}).get("duration")
    media_base = f"/media/{hashid}"
    # Append ?via=<share_id> to the embed video URL when this video is being
    # rendered inside a gallery share — so og:video / twitter:player:stream
    # carry the share context needed for media authorization (otherwise a
    # non-public scene in a public gallery share would 403 for crawlers).
    via_qs = f"?via={via_share_id}" if via_share_id else ""
    if should_embed_full(None, _duration, _size):
        embed_video_url = f"{BASE_DOMAIN}{media_base}/full.mp4{via_qs}"
    else:
        embed_video_url = f"{BASE_DOMAIN}{media_base}/stream.mp4{via_qs}"

    # Canonical URL = the URL the viewer is actually on.
    # For /v/{slug} (global route) → /v/{slug}
    # For /{slug} (individual share) → /{slug}
    if page_slug is not None:
        canonical_url = f"{BASE_DOMAIN}/{page_slug}"
    else:
        canonical_url = f"{BASE_DOMAIN}/v/{slug_for_context}"
    context = site_context(request)
    context.update(
        video_name=video_details.get("title") or "Video",
        hashid=hashid,
        video_details=video_details,
        embed_video_url=embed_video_url,
        hit_count=hit_count,
        canonical_url=canonical_url,
        verify_action=f"{verify_action_prefix}/{slug_for_context}/verify" if verify_action_prefix else f"/{slug_for_context}/verify",
        via_share_id=via_share_id,
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

    # No canonicalisation redirect within /v/: both /v/{sqid} and
    # /v/{vanity_slug} render directly.  The template canonical/og:url
    # matches the URL the viewer actually requested.

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

    # origin="global" never returns PASSWORD_REQUIRED: a PUBLIC/LISTED scene
    # plays freely without prompting, and unlisted/hidden scenes are NOT_FOUND.

    return await _render_video_page(request, db, stash_video_id, override, sqid,
                                    verify_action_prefix="/v",
                                    page_slug=f"v/{slug}")


# ---------------------------------------------------------------------------
# Password verification endpoints
# ---------------------------------------------------------------------------
@router.post("/v/{slug}/verify")
async def verify_video_password(slug: str, password: str = Form(...),
                                next_url: str = Form(None, alias="next"),
                                db: Session = Depends(get_db)):
    """Verify a password for the global /v/ scene route. On success, set the
    scene-keyed unlock cookie and 303 back to the requested page (or /v/{slug})."""
    resolved = _resolve_slug(db, slug)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Video not found")
    stash_video_id, override = resolved
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
    resp = RedirectResponse(safe_next or f"/v/{slug}", status_code=303)
    access.set_unlock_cookie(resp, str(stash_video_id))
    return resp


