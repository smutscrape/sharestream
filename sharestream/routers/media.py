"""All media routes.

Canonical media lives under ``/media/{sqid}/...`` (HLS playlist + segments,
MP4 preview, full MP4, animated WebP, social thumbnails, cached screenshot).
The route parameter is the video's Hashid (Sqids encoding), NOT the raw
Stash scene id — an ``{sqid}`` is decoded to the internal Stash id before any
media proxy or access check. This guarantees no sequential id ever leaks into
a URL, a 301, or a response header, so guessing adjacent scenes is infeasible.

Every canonical route gates on the decoded Stash id via
``access.authorize_scene_media`` before proxying, then delegates to the
``media_proxy.*`` helpers (which take a ``stash_video_id`` + resolution).

The legacy per-share / per-tag-video media paths are kept as **301 redirect
shims** to their canonical ``/media/{sqid}/...`` target.

Resolution is no longer keyed per-URL; it defaults to ``DEFAULT_RESOLUTION``.
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
from sharestream.services.slugs import decode_video_id
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


def _sqid_to_sid(sqid: str) -> int | None:
    """Decode a Hashid slug to a Stash scene id, or None if the slug is not a
    canonical Hashid. The route parameter is an ``{sqid}`` — never a raw id."""
    if sqid.isdigit():
        # The legacy path still permits raw numeric ids; 404 those to stop
        # guessing. Decode via the same canonical re-encode check the Hashid lib
        # uses, so non-canonical padded/aliased forms fail too.
        return None
    sid = decode_video_id(sqid)
    return sid


# ------------------------------------------------------------------
# Legacy → canonical redirect helpers
# ------------------------------------------------------------------
def _legacy_redirect(request: Request, share_id: str, suffix: str) -> RedirectResponse:
    """301 a legacy ``/share/{share_id}/<suffix>`` media URL to the canonical
    ``/media/{sqid}/<suffix>``, carrying the unlock cookie."""
    with SessionLocal() as db:
        resolved = resolve_media(db, share_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="Share link not found")
    from sharestream.services.slugs import encode_video_id
    sqid = encode_video_id(resolved.stash_video_id)
    resp = RedirectResponse(url=f"/media/{sqid}/{suffix}", status_code=301)
    access.carry_unlock_cookie(request, resp, resolved.cookie_share_id, resolved.stash_video_id)
    return resp


def _legacy_tag_redirect(request: Request, share_id: str, video_id: int, suffix: str) -> RedirectResponse:
    """301 a legacy ``/tag/{share_id}/video/{video_id}/<suffix>`` media URL to the
    canonical ``/media/{sqid}/<suffix>``, carrying the unlock cookie."""
    from sharestream.services.slugs import encode_video_id
    sqid = encode_video_id(video_id)
    resp = RedirectResponse(url=f"/media/{sqid}/{suffix}", status_code=301)
    access.carry_unlock_cookie(request, resp, share_id, video_id)
    return resp


# ------------------------------------------------------------------
# Visibility-driven cache-header helpers
# ------------------------------------------------------------------
def _apply_video_cache(resp, cacheable: bool):
    """For format-stable video byte responses: ``public`` when cacheable, else
    ``private, no-store``."""
    resp.headers["Cache-Control"] = "public" if cacheable else "private, no-store"
    return resp


def _tighten_if_private(resp, cacheable: bool):
    """For negotiated image responses (thumb/webp): leave the helper's own
    ``private`` + ``Vary`` header when cacheable; force ``no-store`` when not."""
    if not cacheable:
        resp.headers["Cache-Control"] = "private, no-store"
    return resp


# ------------------------------------------------------------------
# Canonical scene media: /media/{sqid}/...
# ------------------------------------------------------------------
@router.get("/media/{sqid}/stream.m3u8")
async def serve_scene_m3u8(sqid: str, request: Request = None):
    sid = _sqid_to_sid(sqid)
    if sid is None:
        raise HTTPException(status_code=404, detail="Video not found")
    try:
        cacheable = await access.authorize_scene_media(request, sid)
        m3u8_path = SHARES_DIR / f"{sqid}.m3u8"
        if not m3u8_path.exists():
            logger.warning(f".m3u8 not found for sqid={sqid} (sid={sid}), generating")
            if not await media_proxy.generate_m3u8_file(sqid, sid, DEFAULT_RESOLUTION):
                raise HTTPException(status_code=500, detail="Failed to generate .m3u8 file")
        headers = dict(_M3U8_HEADERS)
        headers["Cache-Control"] = "public, max-age=10" if cacheable else "private, no-store"
        return FileResponse(m3u8_path, media_type="application/x-mpegURL", headers=headers)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving .m3u8 file: {e}")
        raise HTTPException(status_code=500, detail="Failed to serve .m3u8 file")


@router.get("/media/{sqid}/stream/{segment}", response_class=StreamingResponse)
async def proxy_scene_segment(sqid: str, segment: str, request: Request = None):
    sid = _sqid_to_sid(sqid)
    if sid is None:
        raise HTTPException(status_code=404, detail="Video not found")
    try:
        cacheable = await access.authorize_scene_media(request, sid)
        return _apply_video_cache(
            await media_proxy.stream_segment(sid, segment, DEFAULT_RESOLUTION), cacheable)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error proxying HLS segment: {e}")
        raise HTTPException(status_code=500, detail="Failed to proxy HLS segment")


@router.get("/media/{sqid}/preview")
async def proxy_scene_preview(sqid: str, request: Request = None):
    sid = _sqid_to_sid(sqid)
    if sid is None:
        raise HTTPException(status_code=404, detail="Video not found")
    cacheable = await access.authorize_scene_media(request, sid)
    return _apply_video_cache(await media_proxy.stream_simple_preview(sid), cacheable)


@router.api_route("/media/{sqid}/stream.mp4", methods=["GET", "HEAD"])
async def serve_scene_mp4(sqid: str, request: Request):
    sid = _sqid_to_sid(sqid)
    if sid is None:
        raise HTTPException(status_code=404, detail="Video not found")
    cacheable = await access.authorize_scene_media(request, sid)
    return _apply_video_cache(await media_proxy.proxy_preview(sid, request), cacheable)


@router.api_route("/media/{sqid}/full.mp4", methods=["GET", "HEAD"])
async def serve_scene_full_mp4(sqid: str, request: Request):
    sid = _sqid_to_sid(sqid)
    if sid is None:
        raise HTTPException(status_code=404, detail="Video not found")
    cacheable = await access.authorize_scene_media(request, sid)
    return _apply_video_cache(await media_proxy.proxy_full(sid, DEFAULT_RESOLUTION, request), cacheable)


@router.api_route("/media/{sqid}/webp", methods=["GET", "HEAD"])
async def serve_scene_webp(sqid: str, request: Request):
    sid = _sqid_to_sid(sqid)
    if sid is None:
        raise HTTPException(status_code=404, detail="Video not found")
    cacheable = await access.authorize_scene_media(request, sid)
    return _tighten_if_private(await media_proxy.proxy_webp(sid, request), cacheable)


@router.api_route("/media/{sqid}/thumb", methods=["GET", "HEAD"])
async def serve_scene_thumb(sqid: str, request: Request):
    sid = _sqid_to_sid(sqid)
    if sid is None:
        raise HTTPException(status_code=404, detail="Video not found")
    cacheable = await access.authorize_scene_media(request, sid)
    return _tighten_if_private(await media_proxy.proxy_thumb(sid, request), cacheable)


@router.get("/media/{sqid}/thumbnail.jpg")
async def serve_scene_thumbnail(sqid: str, request: Request = None, placeholder: bool = True):
    """Cached screenshot served through the access gate (never a redirect to a
    public /static URL, which would sidestep the gate). The player passes
    ?placeholder=false so a missing screenshot leaves the player black instead
    of flashing the "No preview available" tile."""
    sid = _sqid_to_sid(sqid)
    if sid is None:
        raise HTTPException(status_code=404, detail="Video not found")
    cacheable = await access.authorize_scene_media(request, sid)
    thumbnail_path = await fetch_and_cache_thumbnail(str(sid), sid)
    if thumbnail_path:
        cc = "public, max-age=300" if cacheable else "private, no-store"
        return FileResponse(thumbnail_path, media_type="image/jpeg",
                            headers={"Cache-Control": cc})
    if not placeholder:
        raise HTTPException(status_code=404, detail="No thumbnail available")
    return RedirectResponse(url="/static/default_thumbnail.jpg", status_code=302)


# ------------------------------------------------------------------
# Legacy numeric /media/{id}/... → 301 to /media/{sqid}/...
# (so any externally-shared numeric URLs carry forward)
# ------------------------------------------------------------------
@router.get("/media/{stash_video_id:int}/stream.m3u8")
async def redirect_numeric_m3u8(stash_video_id: int, request: Request = None):
    from sharestream.services.slugs import encode_video_id
    sqid = encode_video_id(stash_video_id)
    return RedirectResponse(url=f"/media/{sqid}/stream.m3u8", status_code=301)


@router.get("/media/{stash_video_id:int}/stream/{segment}", response_class=StreamingResponse)
async def redirect_numeric_segment(stash_video_id: int, segment: str, request: Request = None):
    from sharestream.services.slugs import encode_video_id
    sqid = encode_video_id(stash_video_id)
    return RedirectResponse(url=f"/media/{sqid}/stream/{segment}", status_code=301)


@router.get("/media/{stash_video_id:int}/preview")
async def redirect_numeric_preview(stash_video_id: int, request: Request = None):
    from sharestream.services.slugs import encode_video_id
    sqid = encode_video_id(stash_video_id)
    return RedirectResponse(url=f"/media/{sqid}/preview", status_code=301)


@router.api_route("/media/{stash_video_id:int}/stream.mp4", methods=["GET", "HEAD"])
async def redirect_numeric_mp4(stash_video_id: int, request: Request):
    from sharestream.services.slugs import encode_video_id
    sqid = encode_video_id(stash_video_id)
    return RedirectResponse(url=f"/media/{sqid}/stream.mp4", status_code=301)


@router.api_route("/media/{stash_video_id:int}/full.mp4", methods=["GET", "HEAD"])
async def redirect_numeric_full(stash_video_id: int, request: Request):
    from sharestream.services.slugs import encode_video_id
    sqid = encode_video_id(stash_video_id)
    return RedirectResponse(url=f"/media/{sqid}/full.mp4", status_code=301)


@router.api_route("/media/{stash_video_id:int}/webp", methods=["GET", "HEAD"])
async def redirect_numeric_webp(stash_video_id: int, request: Request):
    from sharestream.services.slugs import encode_video_id
    sqid = encode_video_id(stash_video_id)
    return RedirectResponse(url=f"/media/{sqid}/webp", status_code=301)


@router.api_route("/media/{stash_video_id:int}/thumb", methods=["GET", "HEAD"])
async def redirect_numeric_thumb(stash_video_id: int, request: Request):
    from sharestream.services.slugs import encode_video_id
    sqid = encode_video_id(stash_video_id)
    return RedirectResponse(url=f"/media/{sqid}/thumb", status_code=301)


@router.get("/media/{stash_video_id:int}/thumbnail.jpg")
async def redirect_numeric_thumbnail(stash_video_id: int, request: Request = None,
                                     placeholder: bool = True):
    from sharestream.services.slugs import encode_video_id
    sqid = encode_video_id(stash_video_id)
    suffix = "thumbnail.jpg" + ("" if placeholder else "?placeholder=false")
    return RedirectResponse(url=f"/media/{sqid}/{suffix}", status_code=301)


# ------------------------------------------------------------------
# Collection (tag-share) social thumbnail — gallery-scoped, NOT a single scene.
# Stays on its own route; unchanged.
# ------------------------------------------------------------------
@router.api_route("/tag/{share_id}/collection-thumb", methods=["GET", "HEAD"])
async def serve_collection_thumb(share_id: str, request: Request):
    """Negotiated social-embed thumbnail for a tag (collection) share."""
    with SessionLocal() as db:
        tag_share = access.authorize_tag_share(request, db, share_id)
        stash_tag_id = tag_share.stash_tag_id
        password_hash = tag_share.password_hash
        show_in_gallery = tag_share.show_in_gallery
        apply_limit_tag = tag_share.apply_limit_tag

    respect_limit = access.tag_share_respects_limit_tag(password_hash, show_in_gallery, apply_limit_tag)
    videos, _ = await get_videos_by_tag(stash_tag_id, respect_limit_tag=respect_limit)
    video_ids = [int(v["id"]) for v in videos]

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
        return RedirectResponse(url="/og/site-thumbnail", status_code=302)

    headers = {"Cache-Control": "private, max-age=300", "Vary": "Accept, User-Agent"}
    if request.method == "HEAD":
        return Response(status_code=200, media_type=media_type, headers=headers)
    return FileResponse(path, media_type=media_type, headers=headers)


# ------------------------------------------------------------------
# Legacy media shims → 301 to canonical /media/{sqid}/...
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
