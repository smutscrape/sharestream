"""Animated-GIF thumbnails for clients that can't render animated WebP.

The negotiated ``/thumb`` route normally serves Stash's animated WebP. Some
crawlers — notably Matrix's ``matrix-media-repo`` — fetch that URL but store the
WebP without animating it (and re-encode it poorly), so Matrix link previews end
up showing a broken/static frame. For those clients we transcode the WebP to an
animated GIF instead: downscaled to 400px wide, at half the source frame rate,
and squeezed under a 3 MB ceiling via a quality-degradation ladder.

Like ``collection_thumbnails`` this TTL-caches the result to the private shares
dir and returns the on-disk Path (served only through the access-gated route).
ffmpeg can't decode Stash's animated WebP, so the frame work is done with Pillow
on a worker thread; the Stash fetch is async.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
from pathlib import Path
from typing import Optional

from PIL import Image

from sharestream.backends.stash import webp_url
from sharestream.config import COLLECTION_THUMBNAIL_TTL_SECONDS, SHARES_DIR
from sharestream.core.http_client import http_client

logger = logging.getLogger(__name__)

# Target output geometry / size.
TARGET_WIDTH = 400
MAX_BYTES = 3_000_000  # 3 MB ceiling (decimal, so it's under by either MB/MiB reading)

# Degradation ladder, tried in order until the GIF fits under MAX_BYTES.
# (frame_step, colors): frame_step=2 keeps every 2nd frame (half rate, the
# baseline requirement); higher steps thin further, fewer colors shrink the
# palette. The last rung is the smallest we'll produce — it's served even if it
# overshoots, since some clients still prefer an oversized GIF to a static WebP.
_LADDER = [
    (2, 128),
    (2, 64),
    (2, 32),
    (3, 48),
    (3, 32),
    (4, 32),
]

# One build lock per scene id so a crawler burst doesn't transcode the same
# scene many times in parallel. Touched only from the event loop.
_locks: dict[int, asyncio.Lock] = {}


def _lock_for(key: int) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


def _is_fresh(path: Path) -> bool:
    try:
        return path.exists() and (time.time() - path.stat().st_mtime) < COLLECTION_THUMBNAIL_TTL_SECONDS
    except OSError:
        return False


def _build_gif(webp_bytes: bytes, out_path: Path) -> bool:
    """Transcode animated-WebP bytes to a GIF that fits under MAX_BYTES.

    Decodes every WebP frame once, scales to TARGET_WIDTH, then walks the
    degradation ladder re-encoding from the in-memory frames until one rung
    lands under the size ceiling (or the last rung is reached)."""
    try:
        src = Image.open(io.BytesIO(webp_bytes))
        n_frames = getattr(src, "n_frames", 1)
        frames: list[Image.Image] = []
        durations: list[int] = []
        for i in range(n_frames):
            src.seek(i)
            frames.append(src.convert("RGB").copy())
            # Stash previews don't always carry per-frame durations; 40ms ~= 25fps.
            durations.append(src.info.get("duration") or 40)
    except Exception as e:
        logger.warning(f"GIF thumb: failed to decode source webp: {e}")
        return False

    if not frames:
        return False

    # Scale all frames to TARGET_WIDTH up front (even height for safety).
    w, h = frames[0].size
    nw = TARGET_WIDTH if w > TARGET_WIDTH else w
    nh = max(2, round(h * nw / w))
    nh -= nh % 2
    if (nw, nh) != (w, h):
        frames = [f.resize((nw, nh), Image.LANCZOS) for f in frames]

    tmp = out_path.with_suffix(".gif.tmp")
    last_size = None
    for step, colors in _LADDER:
        kept: list[Image.Image] = []
        kept_dur: list[int] = []
        for i in range(0, len(frames), step):
            kept.append(frames[i])
            # Fold the dropped frames' durations in so playback speed is preserved.
            kept_dur.append(sum(durations[i:i + step]))
        # Quantize every frame against one shared palette so the GIF's global
        # color table stays small and frames diff cleanly.
        palette = kept[0].quantize(colors=colors, method=Image.MEDIANCUT)
        q = [f.quantize(colors=colors, palette=palette, dither=Image.FLOYDSTEINBERG) for f in kept]
        try:
            q[0].save(tmp, format="GIF", save_all=True, append_images=q[1:],
                      duration=kept_dur, loop=0, optimize=True, disposal=2)
        except Exception as e:
            logger.warning(f"GIF thumb: encode failed (step={step}, colors={colors}): {e}")
            tmp.unlink(missing_ok=True)
            return False
        size = tmp.stat().st_size
        last_size = size
        if size <= MAX_BYTES:
            break

    try:
        tmp.replace(out_path)
    except OSError as e:
        logger.warning(f"GIF thumb: failed to finalize {out_path}: {e}")
        tmp.unlink(missing_ok=True)
        return False
    if last_size and last_size > MAX_BYTES:
        logger.info(f"GIF thumb {out_path.name}: {last_size} bytes still over 3MB after full ladder")
    return True


async def fetch_and_cache_gif_thumb(stash_video_id: int) -> Optional[Path]:
    """Return the private path to a cached animated GIF for a scene, building it
    from the Stash WebP on a cache miss. Returns None if the source is missing."""
    out_path = SHARES_DIR / f"gif-thumb-{stash_video_id}.gif"
    if _is_fresh(out_path):
        return out_path

    async with _lock_for(stash_video_id):
        # Re-check after acquiring: a concurrent caller may have just built it.
        if _is_fresh(out_path):
            return out_path
        try:
            resp = await http_client.get(webp_url(stash_video_id))
        except Exception as e:
            logger.warning(f"GIF thumb: webp fetch error for {stash_video_id}: {e}")
            return None
        if resp.status_code != 200 or not resp.content:
            logger.warning(f"GIF thumb: webp fetch failed for {stash_video_id}: status={resp.status_code}")
            return None
        ok = await asyncio.to_thread(_build_gif, resp.content, out_path)
        return out_path if ok else None


# Build locks for collection GIFs are keyed by share id (a string), separate
# from the per-scene int keys above.
_collection_locks: dict[str, asyncio.Lock] = {}


def _collection_lock_for(key: str) -> asyncio.Lock:
    lock = _collection_locks.get(key)
    if lock is None:
        lock = _collection_locks[key] = asyncio.Lock()
    return lock


async def build_and_cache_collection_gif(share_id: str, video_ids) -> Optional[Path]:
    """Return the private path to a cached animated GIF for a tag (collection)
    share, transcoded from the share's montage WebP. Returns None if the WebP
    can't be built."""
    # Imported here to avoid a circular import (collection_thumbnails has no
    # dependency on this module, so the edge only goes one way at call time).
    from sharestream.services.collection_thumbnails import build_collection_webp

    out_path = SHARES_DIR / f"gif-collection-{share_id}.gif"
    if _is_fresh(out_path):
        return out_path

    async with _collection_lock_for(share_id):
        if _is_fresh(out_path):
            return out_path
        webp_path = await build_collection_webp(share_id, video_ids)
        if not webp_path:
            return None
        try:
            webp_bytes = await asyncio.to_thread(webp_path.read_bytes)
        except OSError as e:
            logger.warning(f"GIF thumb: failed to read collection webp {webp_path}: {e}")
            return None
        ok = await asyncio.to_thread(_build_gif, webp_bytes, out_path)
        return out_path if ok else None
