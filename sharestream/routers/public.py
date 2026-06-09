"""Public landing pages: the home gallery and the by-tag-name gallery."""
from __future__ import annotations

import logging
from urllib.parse import unquote_plus

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sharestream.core.templates import render
from sharestream.db.session import get_db
from sharestream.services.galleries import build_home_context, build_tag_name_gallery_context

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, sort: str = 'date', page: int = 1, db: Session = Depends(get_db)):
    # Root endpoint for home page showing all available content
    try:
        context = await build_home_context(db, request, sort, page=page)
        return HTMLResponse(content=render("home.html", **context))
    except Exception as e:
        logger.error(f"Error displaying gallery: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to display gallery")


@router.get("/gallery/tag/{tag_name}", response_class=HTMLResponse)
async def gallery_by_tag(tag_name: str, request: Request = None, db: Session = Depends(get_db)):
    tag_name = unquote_plus(tag_name)
    try:
        context = await build_tag_name_gallery_context(db, tag_name)
        return HTMLResponse(content=render("gallery.html", **context))
    except HTTPException:
        # Let 404 (unknown tag) etc. propagate to the themed error page.
        raise
    except Exception as e:
        logger.error(f"Error displaying tag gallery for '{tag_name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to display tag gallery")
