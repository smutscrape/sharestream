"""Short, public-facing share URLs.

These catch-all routes are deliberately the LAST routes registered (this router
is included last in ``main``), so every explicit route always takes precedence.
Custom slugs are validated against RESERVED_SLUGS at creation time, so a slug
can never permanently shadow (or be shadowed by) a real route.

    /{tag}/{video_id}  -> a specific video inside a shared tag
    /{slug}            -> an individual share (VideoOverride), a tag share's
                          gallery, or a static Markdown page (data/pages/{slug}.md)

The legacy ``/share/...`` and ``/tag/...`` URLs still work, so existing links keep
functioning; these just provide the shorter canonical forms.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from sharestream.core.security import pwd_context
from sharestream.core.templates import render
from sharestream.db.models import SharedTag, SharedVideo, VideoOverride
from sharestream.db.session import get_db
from sharestream.routers.pages import render_markdown_page
from sharestream.routers.tags import tag_share_page
from sharestream.services import access
from sharestream.services.slugs import canonical_video_slug

router = APIRouter()


@router.get("/{tag}/{video_id:int}", response_class=HTMLResponse, response_model=None)
async def short_tag_video(tag: str, video_id: int, request: Request = None,
                          db: Session = Depends(get_db)):
    """Legacy short tag-video URL (/{tag}/{video_id}) -> 301 to canonical /v/{slug}.
    Validates the tag share + membership so a bad link 404s rather than
    redirecting to an arbitrary scene, then carries the unlock cookie."""
    tag_share = db.query(SharedTag).filter_by(share_id=tag).first()
    if not tag_share:
        raise HTTPException(status_code=404, detail="Not found")
    access.ensure_not_expired(tag_share.expires_at, "Tag share has expired")
    respect_limit = access.tag_share_respects_limit_tag(tag_share.password_hash,
                                                        tag_share.show_in_gallery,
                                                        tag_share.apply_limit_tag)
    if not await access.is_video_in_tag(tag_share.stash_tag_id, video_id,
                                        respect_limit_tag=respect_limit):
        raise HTTPException(status_code=404, detail="Not found")
    slug = canonical_video_slug(db, video_id)
    resp = RedirectResponse(url=f"/v/{slug}", status_code=301)
    access.carry_unlock_cookie(request, resp, tag, video_id)
    return resp


@router.get("/{slug}", response_class=HTMLResponse, response_model=None)
async def short_share(slug: str, request: Request = None, db: Session = Depends(get_db)):
    """Dispatch a bare-``/{slug}`` URL to the right renderer.

    Priority order:
      1. ``VideoOverride.vanity_slug``        -> individual-share landing page
         (renders the player directly at /{slug}; password_hash governs).
      2. ``SharedTag.share_id``                -> curated tag-share gallery.
      3. Markdown page at data/pages/{slug}.md.
      4. Legacy ``SharedVideo.share_id`` share -> 301 to /v/{canonical} (carries
         the unlock cookie so a viewer who clicked an old link stays unlocked).
      5. Else 404.
    """
    # 1. Individual share via VideoOverride (the canonical home for any share
    # created with a custom slug and/or password).
    override = db.query(VideoOverride).filter(VideoOverride.vanity_slug == slug).first()
    if override is not None:
        return await _render_individual_share(request, db, override, slug)

    # 2. Tag-share gallery (curated collection page at /{share_id}).
    if db.query(SharedTag).filter_by(share_id=slug).first() is not None:
        page = 1
        sort = None  # absent -> let tag_share_page apply the per-share/config default
        if request is not None:
            try:
                page = int(request.query_params.get('page', 1))
            except (TypeError, ValueError):
                page = 1
            sort = request.query_params.get('sort')
        return await tag_share_page(share_id=slug, request=request, page=page, sort=sort, db=db)

    # 3. Static Markdown page at /{slug}.md.
    page = render_markdown_page(slug, request=request)
    if page is not None:
        return page

    # 4. Legacy plain share (SharedVideo row, no custom slug/password) -> 301
    #    to the canonical stateless /v/{slug}.
    video = db.query(SharedVideo).filter_by(share_id=slug).first()
    if video is not None:
        canonical = canonical_video_slug(db, video.stash_video_id)
        resp = RedirectResponse(url=f"/v/{canonical}", status_code=301)
        access.carry_unlock_cookie(request, resp, slug, video.stash_video_id)
        return resp

    # 5. Unknown -> 404.
    raise HTTPException(status_code=404, detail="Not found")


async def _render_individual_share(request, db, override, slug):
    """Render the player for an individual-share landing page at /{slug}.

    Access is governed by the override's password_hash (stash tag visibility is
    ignored — the slug itself is the capability). On success the player template
    is rendered at this URL with media paths keyed to the Hashid (non-sequential)
    — NOT the vanity slug, which may not be a valid Hashid.
    """
    from sharestream.routers.video import _render_video_page
    from sharestream.services.slugs import encode_video_id

    stash_video_id = int(override.stash_video_id)
    # The template needs the Hashid (Sqids) for /media/{sqid}/... URLs — the
    # vanity slug is arbitrary human-readable text and may not decode.
    sqid = encode_video_id(stash_video_id)
    decision = await access.resolve_scene_access(request, db, stash_video_id, origin="override")
    if decision == access.ACCESS_NOT_FOUND:
        raise HTTPException(status_code=404, detail="Video not found")
    if decision == access.ACCESS_PASSWORD_REQUIRED:
        return access.password_prompt_if_locked(
            request, str(stash_video_id), override.password_hash,
            "Video", verify_action=f"/{slug}/verify",
        )
    return await _render_video_page(request, db, stash_video_id, override, sqid,
                                    verify_action_prefix="")


@router.post("/{slug}/verify")
async def verify_individual_share_password(slug: str, password: str = Form(...),
                                          next_url: str = Form(None, alias="next"),
                                          request: Request = None,
                                          db: Session = Depends(get_db)):
    """Verify a password for the individual-share ``/{slug}`` route. On success,
    set the scene-keyed unlock cookie and 303 back to ``/{slug}``."""
    override = db.query(VideoOverride).filter(VideoOverride.vanity_slug == slug).first()
    if override is None:
        raise HTTPException(status_code=404, detail="Video not found")
    stash_video_id = int(override.stash_video_id)
    safe_next = access.safe_next_path(next_url)
    if not override.password_hash or not pwd_context.verify(password, override.password_hash):
        from sharestream.core.branding import site_context
        html = render(
            "password-prompt.html",
            **site_context(request),
            video_name="Video",
            share_id=str(stash_video_id),
            error_message="Incorrect password. Please try again.",
            next_url=safe_next or "",
        )
        return HTMLResponse(html, status_code=401)
    resp = RedirectResponse(safe_next or f"/{slug}", status_code=303)
    access.set_unlock_cookie(resp, str(stash_video_id))
    return resp
