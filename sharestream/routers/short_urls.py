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
from sharestream.db.models import SharedTag, VideoOverride
from sharestream.db.session import get_db
from sharestream.routers.pages import render_markdown_page
from sharestream.routers.tags import tag_share_page
from sharestream.services import access
from sharestream.services.cache import is_video_in_tag
from sharestream.services.slugs import canonical_video_slug, decode_video_id, encode_video_id

router = APIRouter()


@router.get("/{tag_share_id}/{sqid}", response_class=HTMLResponse, response_model=None)
async def gallery_scoped_video(tag_share_id: str, sqid: str, request: Request = None,
                               db: Session = Depends(get_db)):
    """Render video player scoped to a curated Gallery.

    URL: ``/{gallery_slug}/{sqid}``.  Access is validated against that specific
    gallery's password/expiry and video membership.  Media URLs in the player
    template append ``?via=<gallery_share_id>`` so the O(1) media authorization
    path is used (no O(N) tag-share scan).

    MUST be registered BEFORE the catch-all ``/{slug}`` route below."""
    # 1. Validate Sqid
    sid = decode_video_id(sqid)
    if sid is None:
        raise HTTPException(status_code=404, detail="Video not found")

    # 2. Validate Tag Share
    tag_share = db.query(SharedTag).filter(SharedTag.share_id == tag_share_id).first()
    if not tag_share:
        raise HTTPException(status_code=404, detail="Gallery not found")

    # 3. Enforce Access (Expiry, Password, Membership)
    access.ensure_not_expired(tag_share.expires_at, "Gallery has expired")

    locked = access.password_prompt_if_locked(request, tag_share.share_id,
                                              tag_share.password_hash,
                                              f"Gallery: {tag_share.tag_name}",
                                              verify_action=f"/{tag_share_id}/{sqid}/verify")
    if locked is not None:
        return locked

    respect_limit = access.tag_share_respects_limit_tag(tag_share.password_hash,
                                                       tag_share.show_in_gallery,
                                                       tag_share.apply_limit_tag)
    if not await is_video_in_tag(tag_share.stash_tag_id, sid,
                                 respect_limit_tag=respect_limit):
        raise HTTPException(status_code=404, detail="Video not found in this gallery")

    # 4. Render Player (gallery context so media URLs use ?via=)
    from sharestream.routers.video import _render_video_page
    return await _render_video_page(
        request, db, sid, None, sqid,
        verify_action_prefix=f"/{tag_share_id}",
        page_slug=f"{tag_share_id}/{sqid}",
        via_share_id=tag_share_id,
    )


@router.post("/{tag_share_id}/{sqid}/verify")
async def verify_gallery_video_password(tag_share_id: str, sqid: str,
                                       password: str = Form(...),
                                       next_url: str = Form(None, alias="next"),
                                       request: Request = None,
                                       db: Session = Depends(get_db)):
    """Verify a password for the gallery-scoped video route
    ``/{gallery_slug}/{sqid}``. On success, set the tag-share's unlock cookie
    and 303 back to the page."""
    tag_share = db.query(SharedTag).filter(SharedTag.share_id == tag_share_id).first()
    if tag_share is None:
        raise HTTPException(status_code=404, detail="Gallery not found")
    safe_next = access.safe_next_path(next_url)
    if not tag_share.password_hash or not pwd_context.verify(password, tag_share.password_hash):
        from sharestream.core.branding import site_context
        html = render(
            "password-prompt.html",
            **site_context(request),
            video_name=f"Gallery: {tag_share.tag_name}",
            share_id=tag_share_id,
            error_message="Incorrect password. Please try again.",
            next_url=safe_next or "",
            verify_action=f"/{tag_share_id}/{sqid}/verify",
        )
        return HTMLResponse(html, status_code=401)
    resp = RedirectResponse(safe_next or f"/{tag_share_id}/{sqid}", status_code=303)
    access.set_unlock_cookie(resp, tag_share_id)
    return resp


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
    if not await is_video_in_tag(tag_share.stash_tag_id, video_id,
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
      4. Else 404.
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

    # 4. Unknown -> 404.
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
    resp = await _render_video_page(request, db, stash_video_id, override, sqid,
                                    verify_action_prefix="",
                                    page_slug=slug,
                                    via_slug=slug,
                                    custom_title=override.custom_title)
    # Always set the scene-keyed unlock cookie on ALLOW, even when there is no
    # password.  This is what lets an unlisted capability URL (no password) fetch
    # its own media subrequests — authorize_scene_media checks for this cookie
    # regardless of ov_password.
    access.set_unlock_cookie(resp, str(stash_video_id))
    return resp


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
