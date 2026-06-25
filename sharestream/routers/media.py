"""All media routes.

Canonical media lives under ``/media/{stash_video_id}/...`` (HLS playlist +
segments, MP4 preview, full MP4, animated WebP, social thumbnails, cached
screenshot). Every canonical route gates on the scene id via
``access.authorize_scene_media`` before proxying, then delegates to the
``media_proxy.*`` helpers (which take a ``stash_video_id`` + resolution).

The legacy per-share / per-tag-video media paths are kept as **301 redirect
shims** to their canonical ``/media/{id}/...`` target, carrying the unlock
cookie forward so a viewer who unlocked a legacy URL stays unlocked. The only
non-scene media route that stays put is ``/tag/{share_id}/collection-thumb``
(it's a gallery montage, not a single scene).

Resolution is no longer keyed per-URL (a bare scene id has no share row), so it
defaults to ``DEFAULT_RESOLUTION``.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

from sharestream.backends.stash import get_videos_by_tag
from sharestream.config import DEFAULT_RESOLUTION, SHARES_DIR
from sharestream.db.session import SessionLocal
from sharestream.services import access, media_proxy
from sharestream.services.collection_thumbnails import (
    build_collection_collage,
    build_collection_webp,
)
from sharestream.services.gif_thumbnails import build_and_cache_collection_gif
from sharestream.services.resolver import resolve_media
from sharestream.services.thumbnails import fetch_and_cache_thumbnail

logger = logging.getLogger(__name__)

router = APIRouter()


# These routes deliberately do NOT take a ``db: Session = Depends(get_db)``.
# A request-scoped dependency session is held open until the response *finishes*,
# and these routes return StreamingResponses (or proxy media), so a Depends(get_db)
# connection would stay checked out for the entire video download — quickly
# starving the pool under concurrent playback. access.authorize_scene_media owns a
# short-lived session that closes before any network-bound await, and the proxy
# helpers take plain values, so no DB connection is held across streaming.

_M3U8_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "public, max-age=10",
}


# ------------------------------------------------------------------
# Legacy → canonical redirect helpers
# ------------------------------------------------------------------
def _legacy_redirect(request: Request, share_id: str, suffix: str) -> RedirectResponse:
    """301 a legacy ``/share/{share_id}/<suffix>`` or composite media URL to the
    canonical ``/media/{stash_video_id}/<suffix>``, carrying the unlock cookie."""
    with SessionLocal() as db:
        resolved = resolve_media(db, share_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="Share link not found")
    resp = RedirectResponse(url=f"/media/{resolved.stash_video_id}/{suffix}", status_code=301)
    access.carry_unlock_cookie(request, resp, resolved.cookie_share_id, resolved.stash_video_id)
    return resp


def _legacy_tag_redirect(request: Request, share_id: str, video_id: int, suffix: str) -> RedirectResponse:
    """301 a legacy ``/tag/{share_id}/video/{video_id}/<suffix>`` media URL to the
    canonical ``/media/{video_id}/<suffix>``, carrying the unlock cookie (keyed to
    the TAG share id, since that's how the legacy cookie was set)."""
    resp = RedirectResponse(url=f"/media/{video_id}/{suffix}", status_code=301)
    access.carry_unlock_cookie(request, resp, share_id, video_id)
    return resp


# ------------------------------------------------------------------
# Canonical scene media: /media/{stash_video_id}/...
# ------------------------------------------------------------------
# Visibility-driven caching: public/listed scenes with no password serve
# format-stable VIDEO bytes (m3u8, segments, mp4) as `public` (CDN-cacheable);
# unlisted/hidden/password media is forced to `private, no-store` so it never
# lands in a shared cache. The NEGOTIATED thumb/webp routes are a special case:
# they serve different bytes per client (WebP vs JPEG vs GIF), so they keep their
# own `private` + `Vary` headers even when public — promoting them to `public`
# would let a CDN serve one client's format to another. We only TIGHTEN those
# (to no-store) when the scene isn't publicly cacheable.
def _apply_video_cache(resp, cacheable: bool):
    """For format-stable video byte responses: `public` when cacheable, else
    `private, no-store`."""
    resp.headers["Cache-Control"] = "public" if cacheable else "private, no-store"
    return resp


def _tighten_if_private(resp, cacheable: bool):
    """For negotiated image responses (thumb/webp): leave the helper's own
    `private` + `Vary` header when cacheable; force `no-store` when not."""
    if not cacheable:
        resp.headers["Cache-Control"] = "private, no-store"
    return resp


@router.get("/media/{stash_video_id}/stream.m3u8")
async def serve_scene_m3u8(stash_video_id: int, request: Request = None):
    try:
        cacheable = await access.authorize_scene_media(request, stash_video_id)
        m3u8_path = SHARES_DIR / f"{stash_video_id}.m3u8"
        if not m3u8_path.exists():
            logger.warning(f".m3u8 not found for scene {stash_video_id}, generating")
            if not await media_proxy.generate_m3u8_file(str(stash_video_id), stash_video_id, DEFAULT_RESOLUTION):
                raise HTTPException(status_code=500, detail="Failed to generate .m3u8 file")
        headers = dict(_M3U8_HEADERS)
        headers["Cache-Control"] = "public, max-age=10" if cacheable else "private, no-store"
        return FileResponse(m3u8_path, media_type="application/x-mpegURL", headers=headers)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving .m3u8 file: {e}")
        raise HTTPException(status_code=500, detail="Failed to serve .m3u8 file")


@router.get("/media/{stash_video_id}/stream/{segment}", response_class=StreamingResponse)
async def proxy_scene_segment(stash_video_id: int, segment: str, request: Request = None):
    try:
        cacheable = await access.authorize_scene_media(request, stash_video_id)
        return _apply_video_cache(
            await media_proxy.stream_segment(stash_video_id, segment, DEFAULT_RESOLUTION), cacheable)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error proxying HLS segment: {e}")
        raise HTTPException(status_code=500, detail="Failed to proxy HLS segment")


@router.get("/media/{stash_video_id}/preview")
async def proxy_scene_preview(stash_video_id: int, request: Request = None):
    cacheable = await access.authorize_scene_media(request, stash_video_id)
    return _apply_video_cache(await media_proxy.stream_simple_preview(stash_video_id), cacheable)


@router.api_route("/media/{stash_video_id}/stream.mp4", methods=["GET", "HEAD"])
async def serve_scene_mp4(stash_video_id: int, request: Request):
    cacheable = await access.authorize_scene_media(request, stash_video_id)
    return _apply_video_cache(await media_proxy.proxy_preview(stash_video_id, request), cacheable)


@router.api_route("/media/{stash_video_id}/full.mp4", methods=["GET", "HEAD"])
async def serve_scene_full_mp4(stash_video_id: int, request: Request):
    cacheable = await access.authorize_scene_media(request, stash_video_id)
    return _apply_video_cache(await media_proxy.proxy_full(stash_video_id, DEFAULT_RESOLUTION, request), cacheable)


@router.api_route("/media/{stash_video_id}/webp", methods=["GET", "HEAD"])
async def serve_scene_webp(stash_video_id: int, request: Request):
    cacheable = await access.authorize_scene_media(request, stash_video_id)
    return _tighten_if_private(await media_proxy.proxy_webp(stash_video_id, request), cacheable)


@router.api_route("/media/{stash_video_id}/thumb", methods=["GET", "HEAD"])
async def serve_scene_thumb(stash_video_id: int, request: Request):
    cacheable = await access.authorize_scene_media(request, stash_video_id)
    return _tighten_if_private(await media_proxy.proxy_thumb(stash_video_id, request), cacheable)


@router.get("/media/{stash_video_id}/thumbnail.jpg")
async def serve_scene_thumbnail(stash_video_id: int, request: Request = None, placeholder: bool = True):
    # Cached screenshot served through the access gate (never a redirect to a
    # public /static URL, which would sidestep the gate). The player passes
    # ?placeholder=false so a missing screenshot leaves the player black instead
    # of flashing the "No preview available" tile.
    cacheable = await access.authorize_scene_media(request, stash_video_id)
    thumbnail_path = await fetch_and_cache_thumbnail(str(stash_video_id), stash_video_id)
    if thumbnail_path:
        cc = "public, max-age=300" if cacheable else "private, no-store"
        return FileResponse(thumbnail_path, media_type="image/jpeg",
                            headers={"Cache-Control": cc})
    if not placeholder:
        raise HTTPException(status_code=404, detail="No thumbnail available")
    return RedirectResponse(url="/static/default_thumbnail.jpg", status_code=302)


# ------------------------------------------------------------------
# Collection (tag-share) social thumbnail — gallery-scoped, NOT a single scene.
# Stays on its own route; unchanged.
# ------------------------------------------------------------------
@router.api_route("/tag/{share_id}/collection-thumb", methods=["GET", "HEAD"])
async def serve_collection_thumb(share_id: str, request: Request):
    """Negotiated social-embed thumbnail for a tag (collection) share.

    Serves a shuffled montage animated WebP (merged member-video previews) to
    WebP-capable clients, or a collage JPEG (grid of member screenshots) to
    clients that need JPEG (Reddit, Embed.ly, Twitterbot) — same URL, chosen per
    request like ``media_proxy.proxy_thumb``. Falls back to the site thumbnail
    when the collection has no usable source media."""
    with SessionLocal() as db:
        tag_share = access.authorize_tag_share(request, db, share_id)
        stash_tag_id = tag_share.stash_tag_id
        password_hash = tag_share.password_hash
        show_in_gallery = tag_share.show_in_gallery
        apply_limit_tag = tag_share.apply_limit_tag

    # Mirror the share's own surfaces: a featured public share is always limited;
    # a non-public share follows its operator's per-share apply_limit_tag choice.
    respect_limit = access.tag_share_respects_limit_tag(password_hash, show_in_gallery, apply_limit_tag)
    videos, _ = await get_videos_by_tag(stash_tag_id, respect_limit_tag=respect_limit)
    video_ids = [int(v["id"]) for v in videos]

    # matrix-media-repo stores the WebP as a still, so hand it an animated GIF
    # transcoded from the montage WebP. Falls through to WebP if the transcode
    # is unavailable, rather than serving nothing.
    path = media_type = None
    if media_proxy._thumb_prefers_gif(request):
        path = await build_and_cache_collection_gif(share_id, video_ids)
        if path:
            media_type = "image/gif"
    if not path:
        if media_proxy._thumb_prefers_jpeg(request):
            path = await build_collection_collage(share_id, video_ids)
            media_type = "image/jpeg"
        else:
            path = await build_collection_webp(share_id, video_ids)
            media_type = "image/webp"

    if not path:
        # No usable member media (or compose failed): fall back to the site image.
        return RedirectResponse(url="/og/site-thumbnail", status_code=302)

    headers = {"Cache-Control": "private, max-age=300", "Vary": "Accept, User-Agent"}
    if request.method == "HEAD":
        return Response(status_code=200, media_type=media_type, headers=headers)
    return FileResponse(path, media_type=media_type, headers=headers)


# ------------------------------------------------------------------
# Legacy media shims → 301 to canonical /media/{id}/...
# ------------------------------------------------------------------
@router.get("/share/{share_id}/thumbnail.jpg")
async def legacy_share_thumbnail(share_id: str, request: Request = None, placeholder: bool = True):
    suffix = "thumbnail.jpg" + ("" if placeholder else "?placeholder=false")
    return _legacy_redirect(request, share_id, suffix)


@router.get("/share/{share_id}/stream.m3u8")
async def legacy_share_m3u8(share_id: str, request: Request = None):
    return _legacy_redirect(request, share_id, "stream.m3u8")


@router.get("/share/{share_id}/stream/{segment}")
async def legacy_share_segment(share_id: str, segment: str, request: Request = None):
    return _legacy_redirect(request, share_id, f"stream/{segment}")


@router.get("/share/{share_id}/preview")
async def legacy_share_preview(share_id: str, request: Request = None):
    return _legacy_redirect(request, share_id, "preview")


@router.api_route("/share/{share_id}/stream.mp4", methods=["GET", "HEAD"])
async def legacy_share_mp4(share_id: str, request: Request):
    return _legacy_redirect(request, share_id, "stream.mp4")


@router.api_route("/{share_id}/full.mp4", methods=["GET", "HEAD"])
async def legacy_full_mp4(share_id: str, request: Request):
    return _legacy_redirect(request, share_id, "full.mp4")


@router.api_route("/share/{share_id}/webp", methods=["GET", "HEAD"])
async def legacy_share_webp(share_id: str, request: Request):
    return _legacy_redirect(request, share_id, "webp")


@router.api_route("/share/{share_id}/thumb", methods=["GET", "HEAD"])
async def legacy_share_thumb(share_id: str, request: Request):
    return _legacy_redirect(request, share_id, "thumb")


@router.get("/tag/{share_id}/thumbnail/{video_id}")
async def legacy_tag_video_thumbnail(share_id: str, video_id: int, request: Request = None,
                                     placeholder: bool = True):
    suffix = "thumbnail.jpg" + ("" if placeholder else "?placeholder=false")
    return _legacy_tag_redirect(request, share_id, video_id, suffix)


@router.get("/tag/{share_id}/video/{video_id}/stream.m3u8")
async def legacy_tag_video_m3u8(share_id: str, video_id: int, request: Request = None):
    return _legacy_tag_redirect(request, share_id, video_id, "stream.m3u8")


@router.get("/tag/{share_id}/video/{video_id}/stream/{segment}")
async def legacy_tag_video_segment(share_id: str, video_id: int, segment: str, request: Request = None):
    return _legacy_tag_redirect(request, share_id, video_id, f"stream/{segment}")


@router.get("/tag/{share_id}/video/{video_id}/preview")
async def legacy_tag_video_preview(share_id: str, video_id: int, request: Request = None):
    return _legacy_tag_redirect(request, share_id, video_id, "preview")


@router.api_route("/tag/{share_id}/video/{video_id}/stream.mp4", methods=["GET", "HEAD"])
async def legacy_tag_video_mp4(share_id: str, video_id: int, request: Request):
    return _legacy_tag_redirect(request, share_id, video_id, "stream.mp4")


@router.api_route("/tag/{share_id}/video/{video_id}/thumb", methods=["GET", "HEAD"])
async def legacy_tag_video_thumb(share_id: str, video_id: int, request: Request):
    return _legacy_tag_redirect(request, share_id, video_id, "thumb")


@router.api_route("/tag/{share_id}/video/{video_id}/webp", methods=["GET", "HEAD"])
async def legacy_tag_video_webp(share_id: str, video_id: int, request: Request):
    return _legacy_tag_redirect(request, share_id, video_id, "webp")
