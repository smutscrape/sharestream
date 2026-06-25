"""Media proxying: HLS playlist generation and range/HEAD-aware streaming of
Stash media (preview clips, full video, animated WebP, thumbnails, HLS segments)
straight through to the client without storing the bytes on disk.

Streaming rules honored here:
- forward Range / If-Range request headers upstream
- mirror Content-Length / Content-Range / ETag / Last-Modified when present
- never pass None as a response header value
- always close the upstream response in a ``finally``
- HEAD requests mirror headers without streaming a body
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

from sharestream.backends import stash
from sharestream.config import SHARES_DIR
from sharestream.core.http_client import http_client, open_stash_stream, redact_url, segment_headers
from sharestream.services.gif_thumbnails import fetch_and_cache_gif_thumb

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# HLS playlist generation
# ------------------------------------------------------------------
async def generate_m3u8_file(share_id: str, stash_video_id: int, resolution: str) -> bool:
    """Fetch a scene's HLS playlist from Stash, rewrite its segment URLs to our
    access-gated proxy route, and persist it to the private shares dir."""
    stash_url = stash.playlist_url(stash_video_id, resolution)
    try:
        response = await http_client.get(stash_url)
        if response.status_code != 200:
            logger.error(f"Failed to fetch .m3u8 from Stash: status={response.status_code}, url={redact_url(stash_url)}")
            raise Exception(f"Failed to fetch .m3u8: status={response.status_code}")

        # Verify response is a valid .m3u8 playlist
        if not response.text.startswith("#EXTM3U"):
            logger.error(f"Invalid .m3u8 content from Stash: {response.text[:100]}")
            raise Exception("Invalid .m3u8 content")

        # Parse and rewrite .m3u8 playlist
        lines = response.text.splitlines()
        rewritten_lines = []
        for line in lines:
            if line.strip() and not line.startswith("#") and ".ts" in line:
                # Extract segment name (e.g., "0.ts") from any URL, ignoring query parameters
                segment = line.split("/")[-1].split("?")[0]
                # Segments are scene-keyed: rewrite to the canonical /media route
                # (not the legacy /share shim) so playback never eats a redirect
                # hop per segment, regardless of which caller's share_id labels
                # the cached playlist file.
                rewritten_lines.append(f"/media/{stash_video_id}/stream/{segment}")
            else:
                rewritten_lines.append(line)

        # Save rewritten .m3u8 file
        m3u8_path = SHARES_DIR / f"{share_id}.m3u8"
        with open(m3u8_path, "w") as f:
            f.write("\n".join(rewritten_lines) + "\n")
        logger.info(f"Generated .m3u8 file for share_id={share_id} at {m3u8_path}")
        return True
    except Exception as e:
        logger.error(f"Error generating .m3u8 file for share_id={share_id}: {e}")
        return False


# ------------------------------------------------------------------
# Range / HEAD-aware passthrough proxy
# ------------------------------------------------------------------
async def proxy_stash_media(source_url: str, request: Request, media_type: str, label: str,
                            cache_control: str = "public, max-age=3600",
                            vary: str | None = None) -> StreamingResponse:
    """Range-proxy a byte stream from Stash WITHOUT storing anything on disk.

    Range/If-Range request headers are forwarded upstream and the upstream
    Content-Range/Content-Length/ETag are mirrored back, so to a browser or a
    link-preview crawler the response behaves exactly like a static, seekable,
    downloadable file — while the bytes are streamed straight through from Stash
    on demand (nothing is cached to the local filesystem).
    """
    forward_headers = {}
    if "range" in request.headers:
        forward_headers["Range"] = request.headers["range"]
    if "if-range" in request.headers:
        forward_headers["If-Range"] = request.headers["if-range"]

    upstream = await open_stash_stream(source_url, headers=forward_headers)

    if upstream.status_code not in (200, 206):
        logger.error(f"Failed to fetch {label} from Stash: status={upstream.status_code}")
        await upstream.aclose()
        raise HTTPException(status_code=502, detail=f"Upstream error fetching {label}")

    response_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": cache_control,
        "Access-Control-Allow-Origin": "*",
        "Content-Disposition": "inline",
    }
    if vary:
        response_headers["Vary"] = vary
    # httpx's header dict is case-insensitive.
    for h in ("Content-Length", "Content-Range", "Last-Modified", "ETag"):
        value = upstream.headers.get(h)
        if value is not None:
            response_headers[h] = value

    # HEAD: mirror headers without streaming a body, so Lemmy/Mastodon validators
    # see the right Content-Type/Length without downloading.
    if request.method == "HEAD":
        await upstream.aclose()
        return Response(status_code=upstream.status_code, media_type=media_type, headers=response_headers)

    async def iter_body():
        try:
            async for chunk in upstream.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        iter_body(),
        status_code=upstream.status_code,
        media_type=media_type,
        headers=response_headers,
    )


async def proxy_preview(stash_video_id: int, request: Request) -> StreamingResponse:
    """Short Stash preview clip — a small, embed-friendly og:video."""
    return await proxy_stash_media(
        stash.preview_url(stash_video_id),
        request, "video/mp4", "preview",
    )


async def proxy_full(stash_video_id: int, resolution: str, request: Request) -> StreamingResponse:
    """Full video, range-proxied from Stash's direct stream endpoint."""
    return await proxy_stash_media(
        stash.stream_url(stash_video_id, resolution),
        request, "video/mp4", "full video",
    )


async def proxy_webp(stash_video_id: int, request: Request) -> StreamingResponse:
    """Animated WebP preview image, proxied from Stash (never stored on disk)."""
    return await proxy_stash_media(
        stash.webp_url(stash_video_id),
        request, "image/webp", "webp preview",
    )


# Some link-preview scrapers (notably Reddit / Embed.ly) can't render WebP, so
# they fail to grab a thumbnail at all. The negotiated /thumb endpoint serves a
# static JPEG to those clients and the animated WebP to everyone else (Lemmy,
# Mastodon, Discord, browsers). It defaults to WebP so a capable client is
# never downgraded.
_NO_WEBP_UAS = ("redditbot", "reddit", "embedly", "embed.ly", "twitterbot")

# Clients that fetch the WebP but render it as a still (or re-encode it badly).
# We hand these an animated GIF transcoded from the same source instead.
_PREFERS_GIF_UAS = ("matrix-media-repo",)


def _thumb_prefers_jpeg(request: Request) -> bool:
    ua = (request.headers.get("user-agent") or "").lower()
    if any(bot in ua for bot in _NO_WEBP_UAS):
        return True
    accept = request.headers.get("accept") or ""
    # Downgrade on Accept only if the client enumerated image types but not
    # webp (and sent no wildcard) — avoids downgrading clients that send */*.
    if accept and "image/webp" not in accept and "*/*" not in accept and "image/" in accept:
        return True
    return False


def _thumb_prefers_gif(request: Request) -> bool:
    ua = (request.headers.get("user-agent") or "").lower()
    return any(bot in ua for bot in _PREFERS_GIF_UAS)


async def proxy_thumb(stash_video_id: int, request: Request):
    """Animated WebP, animated GIF, or static JPEG thumbnail, chosen per the
    requesting client. `private` + `Vary` keep a shared CDN from serving one
    format to the other."""
    if _thumb_prefers_gif(request):
        gif_path = await fetch_and_cache_gif_thumb(stash_video_id)
        if gif_path:
            headers = {"Cache-Control": "private, max-age=300",
                       "Vary": "Accept, User-Agent",
                       "Access-Control-Allow-Origin": "*"}
            if request.method == "HEAD":
                return Response(status_code=200, media_type="image/gif", headers=headers)
            return FileResponse(gif_path, media_type="image/gif", headers=headers)
        # Transcode unavailable: fall through to WebP rather than nothing.
    if _thumb_prefers_jpeg(request):
        return await proxy_stash_media(
            stash.screenshot_url(stash_video_id),
            request, "image/jpeg", "screenshot",
            cache_control="private, max-age=60", vary="Accept, User-Agent",
        )
    return await proxy_stash_media(
        stash.webp_url(stash_video_id),
        request, "image/webp", "webp preview",
        cache_control="private, max-age=60", vary="Accept, User-Agent",
    )


# ------------------------------------------------------------------
# HLS segment + simple (non-range) preview streaming
# ------------------------------------------------------------------
async def stream_segment(stash_video_id: int, segment: str, resolution: str) -> StreamingResponse:
    """Proxy a single HLS .ts segment from Stash."""
    stash_url = stash.segment_url(stash_video_id, segment, resolution)
    response = await open_stash_stream(stash_url)
    if response.status_code != 200:
        await response.aclose()
        logger.error(f"Failed to fetch HLS segment from Stash: status={response.status_code}, url={redact_url(stash_url)}")
        raise HTTPException(status_code=500, detail="Failed to fetch HLS segment from Stash")

    async def stream_content():
        try:
            async for chunk in response.aiter_bytes(2048 * 1024):
                yield chunk
        finally:
            await response.aclose()

    return StreamingResponse(
        stream_content(),
        media_type="video/mp2t",
        headers=segment_headers(response),
    )


async def stream_simple_preview(stash_video_id: int) -> StreamingResponse:
    """Legacy preview passthrough (no Range support), 1 MiB chunks."""
    preview_url = stash.preview_url(stash_video_id)
    response = await open_stash_stream(preview_url)

    if response.status_code != 200:
        await response.aclose()
        logger.error(f"Failed to fetch preview from Stash: status={response.status_code}")
        raise HTTPException(status_code=500, detail="Failed to fetch preview")

    async def stream_content():
        try:
            async for chunk in response.aiter_bytes(1024 * 1024):
                yield chunk
        finally:
            await response.aclose()

    return StreamingResponse(
        stream_content(),
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600"
        }
    )
