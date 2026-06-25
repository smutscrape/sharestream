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

logger = logging.getLogger(__name__)

router = APIRouter()


@router.api_route("/embed/{stash_video_id}", methods=["GET", "HEAD"],
                  response_class=HTMLResponse, response_model=None)
async def embed_player(stash_video_id: int, request: Request = None, db: Session = Depends(get_db)):
    """Bare, full-bleed player page (no site chrome) for iframe embeds such as
    Mastodon's twitter:player. Keyed to the canonical scene id; gating mirrors
    the /media/{id} routes.

    Password-protected scenes are intentionally NOT embeddable: with no site
    chrome there's nowhere to prompt, and an embed that needs a password defeats
    the purpose of embedding. We return 403 (cookie-gated like the media routes)."""
    await access.authorize_scene_media(request, stash_video_id)

    video_name = "Video"
    details = await get_video_details(stash_video_id)
    if details and details.get("title"):
        video_name = details["title"]

    html = render("video-embed.html", stash_video_id=stash_video_id, video_name=video_name)
    return HTMLResponse(html)
