"""Bare iframe-embed player page (no site chrome) for social embeds."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sharestream.backends.stash import get_video_details
from sharestream.core.templates import render
from sharestream.db.session import get_db
from sharestream.services import access
from sharestream.services.resolver import resolve_media

logger = logging.getLogger(__name__)

router = APIRouter()


@router.api_route("/embed/{share_id}", methods=["GET", "HEAD"],
                  response_class=HTMLResponse, response_model=None)
async def embed_player(share_id: str, request: Request = None, db: Session = Depends(get_db)):
    """Bare, full-bleed player page (no site chrome) for iframe embeds such as
    Mastodon's twitter:player. Accepts an individual share id or the composite
    tag-video id (tag-{tag}-video-{id}); gating mirrors the player routes.

    Password-protected shares are intentionally NOT embeddable: with no site
    chrome there's nowhere to prompt, and an embed that needs a password defeats
    the purpose of embedding. We return 403 (cookie-gated like the media routes)."""
    resolved = resolve_media(db, share_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="Share link not found")
    await access.authorize_media(request, resolved)

    if resolved.is_individual:
        video_name = resolved.title
    else:
        video_name = "Video"
        details = await get_video_details(resolved.stash_video_id)
        if details and details.get("title"):
            video_name = details["title"]

    html = render("video-embed.html", share_id=share_id, video_name=video_name)
    return HTMLResponse(html)
