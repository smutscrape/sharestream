"""Bare iframe-embed player page (no site chrome) for social embeds."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from sharestream.backends.stash import get_video_details
from sharestream.core.templates import render
from sharestream.db.session import get_db
from sharestream.services import access
from sharestream.services.slugs import decode_video_id

logger = logging.getLogger(__name__)

router = APIRouter()


@router.api_route("/embed/{sqid}", methods=["GET", "HEAD"],
                  response_class=HTMLResponse, response_model=None)
async def embed_player(sqid: str, request: Request = None,
                       via: str | None = None, via_slug: str | None = None,
                       db: Session = Depends(get_db)):
    """Bare, full-bleed player page (no site chrome) for iframe embeds such as
    Mastodon's twitter:player. The route parameter is the video's Hashid (Sqids
    encoding); decoded to a Stash scene id before access-checking.

    Password-protected scenes are intentionally NOT embeddable: with no site
    chrome there's nowhere to prompt, and an embed that needs a password defeats
    the purpose of embedding. We return 403 (cookie-gated like the media routes)."""
    sid = decode_video_id(sqid)
    if sid is None:
        raise HTTPException(status_code=404, detail="Video not found")

    await access.authorize_scene_media(request, sid, via_share_id=via, via_slug=via_slug)

    video_name = "Video"
    details = await get_video_details(sid)
    if details and details.get("title"):
        video_name = details["title"]

    # Pass the Hashid (not the Stash id) so template URLs are non-sequential,
    # along with any gallery/slug capability context for media URLs.
    html = render("video-embed.html", hashid=sqid, video_name=video_name, via_share_id=via, via_slug=via_slug)
    return HTMLResponse(html)


# Legacy /embed/{stash_video_id:int} → 301 to /embed/{sqid}
@router.api_route("/embed/{stash_video_id:int}", methods=["GET", "HEAD"])
async def embed_numeric_redirect(stash_video_id: int, request: Request = None,
                                 db: Session = Depends(get_db)):
    from sharestream.services.slugs import encode_video_id
    sqid = encode_video_id(stash_video_id)
    return RedirectResponse(url=f"/embed/{sqid}", status_code=301)
