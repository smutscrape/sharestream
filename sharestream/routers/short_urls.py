"""Short, public-facing share URLs.

These catch-all routes are deliberately the LAST routes registered (this router
is included last in ``main``), so every explicit route always takes precedence.
Custom slugs are validated against RESERVED_SLUGS at creation time, so a slug
can never permanently shadow (or be shadowed by) a real route.

    /{tag}/{video_id}  -> a specific video inside a shared tag
    /{slug}            -> an individual video share, a tag share's gallery, or
                          a static Markdown page (data/pages/{slug}.md)

The legacy /share/... and /tag/... URLs still work, so existing links keep
functioning; these just provide the shorter canonical forms.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from sharestream.db.models import SharedTag, SharedVideo
from sharestream.db.session import get_db
from sharestream.routers.pages import render_markdown_page
from sharestream.routers.tags import tag_share_page
from sharestream.services import access
from sharestream.services.slugs import canonical_video_slug

router = APIRouter()


@router.get("/{tag}/{video_id:int}", response_class=HTMLResponse, response_model=None)
async def short_tag_video(tag: str, video_id: int, request: Request = None,
                          db: Session = Depends(get_db)):
    """Legacy short tag-video URL (/{tag}/{video_id}) → 301 to canonical /v/{slug}.
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
    # A legacy individual-video slug → 301 to the canonical /v/{slug}, carrying
    # the unlock cookie. A curated Gallery (SharedTag) still renders here at
    # /{slug}. Markdown pages and 404 fall through unchanged.
    video = db.query(SharedVideo).filter_by(share_id=slug).first()
    if video is not None:
        canonical = canonical_video_slug(db, video.stash_video_id)
        resp = RedirectResponse(url=f"/v/{canonical}", status_code=301)
        access.carry_unlock_cookie(request, resp, slug, video.stash_video_id)
        return resp

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
    page = render_markdown_page(slug, request=request)
    if page is not None:
        return page
    raise HTTPException(status_code=404, detail="Not found")
