"""Public landing pages: the home gallery and the by-tag-name gallery."""
from __future__ import annotations

import logging
from urllib.parse import unquote_plus

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from fastapi.responses import RedirectResponse

from sharestream.config import DEFAULT_SORT
from sharestream.core.templates import render
from sharestream.db.models import SharedTag
from sharestream.db.session import get_db
from sharestream.services.galleries import (
    build_home_context,
    build_tag_name_gallery_context,
    normalize_sort,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, sort: str | None = None, page: int = 1, db: Session = Depends(get_db)):
    # Root endpoint for home page showing all available content. An explicit
    # ?sort= (from the dropdown) wins; otherwise fall back to the configured
    # default sort mode.
    try:
        effective_sort = normalize_sort(sort) or DEFAULT_SORT
        context = await build_home_context(db, request, effective_sort, page=page)
        return HTMLResponse(content=render("home.html", **context))
    except Exception as e:
        logger.error(f"Error displaying gallery: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to display gallery")


@router.get("/tag/{tag_name}", response_class=HTMLResponse, response_model=None)
async def tag_view(tag_name: str, request: Request = None, page: int = 1,
                   sort: str | None = None, db: Session = Depends(get_db)):
    """Incidental Tag view: all videos sharing a Stash tag, at /tag/{name}.

    Disambiguation: if {name} matches a curated Gallery's share_id, this is a
    legacy Gallery URL → 301 to /{name} (the Gallery's canonical home). Otherwise
    {name} is a Stash tag name and we render the auto-generated tag listing.

    The listing is driven directly by the requested tag's own scene set — it
    does NOT scan public shares to discover videos. Visibility filter: listed
    and public only, never hidden."""
    raw = tag_name  # the legacy /tag/{share_id} used the raw (unescaped) slug
    tag_name = unquote_plus(tag_name)
    # Internal tags (leading underscore, e.g. _public/_listed/_banned) are tooling,
    # not content — never browsable. 404 before any Stash lookup.
    if tag_name.startswith("_"):
        raise HTTPException(status_code=404, detail="Not found")
    # Legacy Gallery URL? (share ids are url-safe, so raw vs unquoted both work.)
    if db.query(SharedTag).filter_by(share_id=raw).first() or \
       db.query(SharedTag).filter_by(share_id=tag_name).first():
        return RedirectResponse(url=f"/{raw}", status_code=301)
    effective_sort = normalize_sort(sort) or DEFAULT_SORT
    try:
        context = await build_tag_name_gallery_context(db, tag_name, request=request,
                                                      page=page, sort=effective_sort)
        return HTMLResponse(content=render("gallery.html", **context))
    except HTTPException:
        # Let 404 (unknown tag) etc. propagate to the themed error page.
        raise
    except Exception as e:
        logger.error(f"Error displaying tag gallery for '{tag_name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to display tag gallery")


@router.get("/gallery/tag/{tag_name}", response_class=RedirectResponse)
async def legacy_gallery_by_tag(tag_name: str):
    """Legacy incidental-tag URL → 301 to the canonical /tag/{name}."""
    return RedirectResponse(url=f"/tag/{tag_name}", status_code=301)
