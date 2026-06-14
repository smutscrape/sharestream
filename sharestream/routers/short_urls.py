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
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sharestream.db.models import SharedTag, SharedVideo
from sharestream.db.session import get_db
from sharestream.routers.pages import render_markdown_page
from sharestream.routers.shares import share_page
from sharestream.routers.tags import tag_share_page, tag_video_page

router = APIRouter()


@router.get("/{tag}/{video_id:int}", response_class=HTMLResponse, response_model=None)
async def short_tag_video(tag: str, video_id: int, request: Request = None,
                          db: Session = Depends(get_db)):
    return await tag_video_page(share_id=tag, video_id=video_id, request=request, db=db)


@router.get("/{slug}", response_class=HTMLResponse, response_model=None)
async def short_share(slug: str, request: Request = None, db: Session = Depends(get_db)):
    is_video = db.query(SharedVideo).filter_by(share_id=slug).first() is not None
    is_tag = (not is_video) and db.query(SharedTag).filter_by(share_id=slug).first() is not None

    if is_video:
        return await share_page(share_id=slug, request=request, db=db)
    if is_tag:
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
