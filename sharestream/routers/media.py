"""All media routes: HLS playlists/segments, MP4 preview, full MP4, animated
WebP, social-embed thumbnails, and the cached screenshot routes.

Every route enforces expiry + password (+ tag membership for tag videos) via the
``services.access`` helpers before proxying or serving anything, so
password-protected content stays gated across all media types. Routes that
accept either an individual share id or the composite ``tag-<id>-video-<n>`` id
use ``services.resolver`` instead of repeating the id-shape parsing inline.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from sharestream.config import SHARES_DIR
from sharestream.db.session import get_db
from sharestream.services import access, media_proxy
from sharestream.services.resolver import resolve_media
from sharestream.services.thumbnails import (
    fetch_and_cache_tag_video_thumbnail,
    fetch_and_cache_thumbnail,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_M3U8_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "public, max-age=10",
}


# ------------------------------------------------------------------
# Cached screenshot routes (served through the access gate)
# ------------------------------------------------------------------
@router.get("/tag/{share_id}/thumbnail/{video_id}")
async def get_tag_video_thumbnail(share_id: str, video_id: int, request: Request = None,
                                  db: Session = Depends(get_db)):
    # Enforce expiry / password / tag membership before serving the cached
    # screenshot straight from the private cache (NOT a redirect to a public
    # /static URL, which would sidestep this gate).
    await access.authorize_tag_video(request, db, share_id, video_id)
    thumbnail_path = await fetch_and_cache_tag_video_thumbnail(share_id, video_id)
    if thumbnail_path:
        return FileResponse(thumbnail_path, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=300"})
    # Fall back to the generic placeholder (not protected content) if the
    # upstream screenshot fetch failed.
    return RedirectResponse(url="/static/default_thumbnail.jpg", status_code=302)


@router.get("/share/{share_id}/thumbnail.jpg")
async def serve_share_thumbnail(share_id: str, request: Request = None,
                                db: Session = Depends(get_db)):
    # Cached screenshot for an individual share, served through the access gate so
    # a password-protected share's thumbnail can't be fetched by guessing a
    # static URL.
    video = access.authorize_share_media(request, db, share_id, "Share has expired")
    thumbnail_path = await fetch_and_cache_thumbnail(share_id, video.stash_video_id)
    if thumbnail_path:
        return FileResponse(thumbnail_path, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=300"})
    return RedirectResponse(url="/static/default_thumbnail.jpg", status_code=302)


# ------------------------------------------------------------------
# HLS playlists + segments
# ------------------------------------------------------------------
@router.get("/share/{share_id}/stream.m3u8")
async def serve_m3u8_file(share_id: str, request: Request = None, db: Session = Depends(get_db)):
    # Accepts an individual share id or the composite tag-video id.
    try:
        resolved = resolve_media(db, share_id)
        if not resolved:
            raise HTTPException(status_code=404, detail="Share link not found")
        await access.authorize_media(request, resolved)

        m3u8_path = SHARES_DIR / f"{share_id}.m3u8"
        if not m3u8_path.exists():
            logger.warning(f".m3u8 file not found for share_id={share_id}, attempting to generate")
            if not await media_proxy.generate_m3u8_file(share_id, resolved.stash_video_id, resolved.resolution):
                logger.error(f"Failed to generate .m3u8 file for share_id={share_id}")
                raise HTTPException(status_code=500, detail="Failed to regenerate .m3u8 file")

        return FileResponse(m3u8_path, media_type="application/x-mpegURL", headers=_M3U8_HEADERS)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving .m3u8 file: {e}")
        raise HTTPException(status_code=500, detail="Failed to serve .m3u8 file")


@router.get("/share/{share_id}/stream/{segment}", response_class=StreamingResponse)
async def proxy_hls_segment(share_id: str, segment: str, request: Request = None,
                            db: Session = Depends(get_db)):
    try:
        resolved = resolve_media(db, share_id)
        if not resolved:
            raise HTTPException(status_code=404, detail="Share link not found")
        if request is not None:
            logger.debug(f"{request.client.host} requested segment {segment} for share_id={share_id}")
        await access.authorize_media(request, resolved)
        return await media_proxy.stream_segment(resolved.stash_video_id, segment, resolved.resolution)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error proxying HLS segment: {e}")
        raise HTTPException(status_code=500, detail="Failed to proxy HLS segment")


@router.get("/tag/{share_id}/video/{video_id}/stream.m3u8")
async def serve_tag_video_m3u8(share_id: str, video_id: int, request: Request = None,
                               db: Session = Depends(get_db)):
    try:
        tag_share = await access.authorize_tag_video(request, db, share_id, video_id)

        composite_id = f"tag-{share_id}-video-{video_id}"
        m3u8_path = SHARES_DIR / f"{composite_id}.m3u8"
        if not m3u8_path.exists():
            logger.warning(f".m3u8 file not found for tag video {share_id}/{video_id}, attempting to generate")
            if not await media_proxy.generate_m3u8_file(composite_id, video_id, tag_share.resolution):
                logger.error(f"Failed to generate .m3u8 file for tag video {share_id}/{video_id}")
                raise HTTPException(status_code=500, detail="Failed to generate .m3u8 file")

        return FileResponse(m3u8_path, media_type="application/x-mpegURL", headers=_M3U8_HEADERS)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving tag video .m3u8 file: {e}")
        raise HTTPException(status_code=500, detail="Failed to serve .m3u8 file")


@router.get("/tag/{share_id}/video/{video_id}/stream/{segment}", response_class=StreamingResponse)
async def proxy_tag_video_segment(share_id: str, video_id: int, segment: str, request: Request = None,
                                  db: Session = Depends(get_db)):
    try:
        if request is not None:
            logger.debug(f"{request.client.host} requested segment {segment} for tag video {share_id}/{video_id}")
        tag_share = await access.authorize_tag_video(request, db, share_id, video_id)
        return await media_proxy.stream_segment(video_id, segment, tag_share.resolution)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error proxying tag video HLS segment: {e}")
        raise HTTPException(status_code=500, detail="Failed to proxy HLS segment")


# ------------------------------------------------------------------
# Legacy preview passthrough routes
# ------------------------------------------------------------------
@router.get("/share/{share_id}/preview")
async def proxy_video_preview(share_id: str, request: Request = None, db: Session = Depends(get_db)):
    video = access.authorize_share_media(request, db, share_id, "Share has expired")
    return await media_proxy.stream_simple_preview(video.stash_video_id)


@router.get("/tag/{share_id}/video/{video_id}/preview")
async def proxy_tag_video_preview(share_id: str, video_id: int, request: Request = None,
                                  db: Session = Depends(get_db)):
    await access.authorize_tag_video(request, db, share_id, video_id)
    return await media_proxy.stream_simple_preview(video_id)


# ------------------------------------------------------------------
# Range/HEAD-aware MP4 / WebP / thumbnail proxy routes
# ------------------------------------------------------------------
@router.api_route("/share/{share_id}/stream.mp4", methods=["GET", "HEAD"])
async def serve_mp4_preview(share_id: str, request: Request, db: Session = Depends(get_db)):
    resolved = resolve_media(db, share_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="Share link not found")
    await access.authorize_media(request, resolved)
    return await media_proxy.proxy_preview(resolved.stash_video_id, request)


@router.api_route("/tag/{share_id}/video/{video_id}/stream.mp4", methods=["GET", "HEAD"])
async def serve_tag_video_mp4(share_id: str, video_id: int, request: Request,
                              db: Session = Depends(get_db)):
    await access.authorize_tag_video(request, db, share_id, video_id)
    return await media_proxy.proxy_preview(video_id, request)


@router.api_route("/{share_id}/full.mp4", methods=["GET", "HEAD"])
async def serve_full_mp4(share_id: str, request: Request, db: Session = Depends(get_db)):
    """Full-video og:video, range-proxied from Stash (never stored on disk).

    Accepts either an individual share id or the composite tag-video id
    (tag-{tag}-video-{id}); gating mirrors the preview/stream routes.
    """
    resolved = resolve_media(db, share_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="Share link not found")
    await access.authorize_media(request, resolved)
    return await media_proxy.proxy_full(resolved.stash_video_id, resolved.resolution, request)


@router.api_route("/share/{share_id}/webp", methods=["GET", "HEAD"])
async def serve_share_webp(share_id: str, request: Request, db: Session = Depends(get_db)):
    """Animated WebP preview for an individual share (or composite tag-video id)."""
    resolved = resolve_media(db, share_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="Share link not found")
    await access.authorize_media(request, resolved)
    return await media_proxy.proxy_webp(resolved.stash_video_id, request)


@router.api_route("/share/{share_id}/thumb", methods=["GET", "HEAD"])
async def serve_share_thumb(share_id: str, request: Request, db: Session = Depends(get_db)):
    """Social-embed thumbnail (animated WebP, or static JPEG for Reddit/Embed.ly)
    for an individual share or composite tag-video id."""
    resolved = resolve_media(db, share_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="Share link not found")
    await access.authorize_media(request, resolved)
    return await media_proxy.proxy_thumb(resolved.stash_video_id, request)


@router.api_route("/tag/{share_id}/video/{video_id}/thumb", methods=["GET", "HEAD"])
async def serve_tag_video_thumb(share_id: str, video_id: int, request: Request,
                                db: Session = Depends(get_db)):
    """Social-embed thumbnail for a video within a tag share."""
    await access.authorize_tag_video(request, db, share_id, video_id)
    return await media_proxy.proxy_thumb(video_id, request)


@router.api_route("/tag/{share_id}/video/{video_id}/webp", methods=["GET", "HEAD"])
async def serve_tag_video_webp(share_id: str, video_id: int, request: Request,
                               db: Session = Depends(get_db)):
    """Animated WebP preview for a video within a tag share."""
    await access.authorize_tag_video(request, db, share_id, video_id)
    return await media_proxy.proxy_webp(video_id, request)
