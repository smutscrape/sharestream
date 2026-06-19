"""Generated social-embed thumbnails for tag (collection) shares.

A tag share points at many videos, so its link-preview image should represent
the *collection*, not a single scene. This module builds two such images from a
share's member videos and TTL-caches them to the private shares dir:

* an **animated WebP** — a justified/masonry collage in which up to 6 member-video
  animated previews play **simultaneously**, each in a tile of its own native
  aspect ratio with a consistent gutter between tiles (for WebP-capable clients:
  Lemmy, Mastodon, Discord, browsers); and
* a **collage JPEG** — the same justified/masonry layout filled with up to 9
  member screenshots (for clients that can't render WebP: Reddit, Embed.ly,
  Twitterbot).

Both formats share one layout algorithm (:func:`_justified_layout`), so the
arrangement and gutters look identical; the only difference is whether each tile
is a looping clip or a still. Tiles keep their source aspect ratio — nothing is
letterboxed or aspect-cropped — and clip frames are never subsampled: the longest
clip plays through once per loop and shorter clips loop to match it.

The negotiated ``/tag/{share_id}/collection-thumb`` route picks which to serve
per request (mirroring ``media_proxy.proxy_thumb``). Pillow work is CPU-bound, so
it runs in a thread; the Stash fetches are async. Like ``thumbnails.py`` these
return the PRIVATE on-disk Path — the bytes are served only through the
access-gated route, never a public /static URL.
"""
from __future__ import annotations

import asyncio
import io
import logging
import random
import time
import statistics
from pathlib import Path
from typing import Optional

from PIL import Image

from sharestream.backends.stash import screenshot_url, webp_url
from sharestream.config import COLLECTION_THUMBNAIL_TTL_SECONDS, SHARES_DIR
from sharestream.core.branding import OG_BG_COLOR
from sharestream.core.http_client import http_client

logger = logging.getLogger(__name__)

# How many member videos feed each format.
MAX_WEBP_VIDEOS = 6
MAX_COLLAGE_VIDEOS = 9

# Shared masonry geometry (px). The layout justifies each full row to fill
# CONTAINER_WIDTH at roughly TARGET_ROW_HEIGHT, separated by GUTTER on all sides.
CONTAINER_WIDTH = 1080
TARGET_ROW_HEIGHT = 300
GUTTER = 6

DEFAULT_FRAME_MS = 100
# Safety ceiling on animated-WebP output frames (never hit by real Stash previews,
# which are short). Bounds file size without subsampling a normal clip.
MAX_OUTPUT_FRAMES = 240

# One build lock per share id so a crawler burst doesn't compose the same image
# many times in parallel. Touched only from the event loop.
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(key: str) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


def _is_fresh(path: Path) -> bool:
    """True if the cached artifact exists and is within the TTL window."""
    try:
        return path.exists() and (time.time() - path.stat().st_mtime) < COLLECTION_THUMBNAIL_TTL_SECONDS
    except OSError:
        return False


async def _fetch_bytes(url: str) -> Optional[bytes]:
    """GET ``url`` and return its body, or None on any failure."""
    try:
        resp = await http_client.get(url)
        if resp.status_code == 200 and resp.content:
            return resp.content
        logger.warning(f"Collection thumb source fetch failed: status={resp.status_code}")
        return None
    except Exception as e:
        logger.warning(f"Collection thumb source fetch error: {e}")
        return None


def _pick(video_ids, count: int) -> list[int]:
    """Shuffle a copy of ``video_ids`` and take up to ``count`` distinct ids."""
    ids = [int(v) for v in dict.fromkeys(video_ids)]  # de-dup, preserve as ints
    random.shuffle(ids)
    return ids[:count]


# ------------------------------------------------------------------
# Shared justified (masonry) layout
# ------------------------------------------------------------------
# A placed tile: where it goes on the canvas and at what size.
class _Tile:
    __slots__ = ("x", "y", "w", "h")

    def __init__(self, x: int, y: int, w: int, h: int):
        self.x, self.y, self.w, self.h = x, y, w, h


def _justified_layout(aspects: list[float]) -> tuple[list[_Tile], int, int]:
    """Pack tiles of the given aspect ratios into a justified (Flickr-style) grid.

    Tiles flow left-to-right into rows; each full row is scaled so its tiles fill
    ``CONTAINER_WIDTH`` exactly at ~``TARGET_ROW_HEIGHT`` tall, with ``GUTTER`` on
    all sides. The number of tiles per row is chosen by aspect so rows stay near
    the target height. Returns (placed tiles, canvas_width, canvas_height).
    """
    n = len(aspects)
    # Choose a tiles-per-row that keeps rows close to the target height. With the
    # small counts here (<=9), aim for a near-square overall composition.
    if n <= 1:
        per_row = 1
    elif n <= 4:
        per_row = 2
    else:
        per_row = 3

    rows: list[list[float]] = [aspects[i:i + per_row] for i in range(0, n, per_row)]

    tiles: list[_Tile] = []
    y = GUTTER
    canvas_w = CONTAINER_WIDTH
    for row in rows:
        # Width available for images in this row (gutters between + outer edges).
        avail = CONTAINER_WIDTH - GUTTER * (len(row) + 1)
        sum_aspect = sum(row) or 1.0
        row_h = max(1, round(avail / sum_aspect))
        x = GUTTER
        for a in row:
            w = max(1, round(row_h * a))
            tiles.append(_Tile(x, y, w, row_h))
            x += w + GUTTER
        # Nudge the last tile so the row's right edge lands exactly on the gutter
        # line (rounding can leave a 1-2px seam otherwise).
        if tiles:
            last = tiles[-1]
            last.w = max(1, CONTAINER_WIDTH - GUTTER - last.x)
        y += row_h + GUTTER
    canvas_h = y
    return tiles, canvas_w, canvas_h


def _cover(frame: Image.Image, w: int, h: int) -> Image.Image:
    """Scale+center-crop ``frame`` to exactly w x h, preserving aspect (cover)."""
    from PIL import ImageOps
    return ImageOps.fit(frame.convert("RGB"), (w, h), method=Image.LANCZOS)


def _compose_collage(records: list[dict], out_path: Path) -> bool:
    """Composite still frames into the shared justified layout as a JPEG."""
    if not records:
        return False
    aspects = [r["aspect"] for r in records]
    tiles, cw, ch = _justified_layout(aspects)
    canvas = Image.new("RGB", (cw, ch), OG_BG_COLOR)
    for rec, t in zip(records, tiles):
        canvas.paste(_cover(rec["frames"][0], t.w, t.h), (t.x, t.y))
    tmp = out_path.with_suffix(".jpg.tmp")
    try:
        canvas.save(tmp, format="JPEG", quality=85, optimize=True)
        tmp.replace(out_path)
        return True
    except Exception as e:
        logger.error(f"Failed to write collection collage {out_path}: {e}")
        tmp.unlink(missing_ok=True)
        return False


def _compose_webp(records: list[dict], out_path: Path) -> bool:
    """Composite looping clips into the shared justified layout as an animated WebP.

    All clips play at once. The output runs on a uniform per-frame tick equal to
    the median source frame duration; the number of output frames matches the
    longest clip (bounded by MAX_OUTPUT_FRAMES). Each clip's own frames are used
    in full — never subsampled — and shorter clips loop to fill the timeline.
    """
    if not records:
        return False
    aspects = [r["aspect"] for r in records]
    tiles, cw, ch = _justified_layout(aspects)

    # Pre-resize every source frame to its tile size once (cover-crop), so the
    # per-output-frame loop is just paste calls.
    resized: list[list[Image.Image]] = []
    for rec, t in zip(records, tiles):
        resized.append([_cover(f, t.w, t.h) for f in rec["frames"]])

    max_len = max(len(fr) for fr in resized)
    total = min(max_len, MAX_OUTPUT_FRAMES)
    # Uniform tick: the median of all source per-frame durations (robust to one
    # clip with odd timing). Falls back to DEFAULT_FRAME_MS.
    all_durs = [d for r in records for d in r["durations"] if d > 0]
    tick = int(statistics.median(all_durs)) if all_durs else DEFAULT_FRAME_MS

    out_frames: list[Image.Image] = []
    for i in range(total):
        canvas = Image.new("RGB", (cw, ch), OG_BG_COLOR)
        for frames, t in zip(resized, tiles):
            canvas.paste(frames[i % len(frames)], (t.x, t.y))
        out_frames.append(canvas)

    tmp = out_path.with_suffix(".webp.tmp")
    try:
        out_frames[0].save(
            tmp, format="WEBP", save_all=True, append_images=out_frames[1:],
            duration=tick, loop=0, quality=70, method=4,
        )
        tmp.replace(out_path)
        return True
    except Exception as e:
        logger.error(f"Failed to write collection WebP {out_path}: {e}")
        tmp.unlink(missing_ok=True)
        return False


# ------------------------------------------------------------------
# Source decoding (bytes -> aspect + frames + per-frame durations)
# ------------------------------------------------------------------
def _decode_sources(sources: list[bytes], animated: bool) -> list[dict]:
    """Decode each source image's frames into in-memory records.

    Returns a list of ``{"aspect", "frames": [Image], "durations": [ms]}``. For
    ``animated`` sources every frame is kept (no subsampling); otherwise only the
    first frame is read. Unreadable sources are skipped.
    """
    from PIL import ImageSequence
    out: list[dict] = []
    for raw in sources:
        try:
            with Image.open(io.BytesIO(raw)) as im:
                w, h = im.size
                aspect = (w / h) if h else 1.0
                frames: list[Image.Image] = []
                durations: list[int] = []
                if animated:
                    for frame in ImageSequence.Iterator(im):
                        frames.append(frame.convert("RGB").copy())
                        durations.append(int(frame.info.get("duration", DEFAULT_FRAME_MS)) or DEFAULT_FRAME_MS)
                else:
                    frames.append(im.convert("RGB").copy())
                    durations.append(DEFAULT_FRAME_MS)
            if frames:
                out.append({"aspect": aspect, "frames": frames, "durations": durations})
        except Exception as e:
            logger.warning(f"Skipping unreadable collection source: {e}")
            continue
    return out


def _build_webp(sources: list[bytes], out_path: Path) -> bool:
    """Thread entry point: decode animated sources, then compose the WebP collage."""
    return _compose_webp(_decode_sources(sources, animated=True), out_path)


def _build_collage(sources: list[bytes], out_path: Path) -> bool:
    """Thread entry point: decode still sources, then compose the JPEG collage."""
    return _compose_collage(_decode_sources(sources, animated=False), out_path)


# ------------------------------------------------------------------
# Async builders (TTL-cached, single-flight per share)
# ------------------------------------------------------------------
async def build_collection_webp(share_id: str, video_ids) -> Optional[Path]:
    """Build (or return cached) the montage animated WebP for a tag share."""
    out_path = SHARES_DIR / f"collection-{share_id}.webp"
    if _is_fresh(out_path):
        return out_path
    async with _lock_for(f"webp:{share_id}"):
        if _is_fresh(out_path):  # another request built it while we waited
            return out_path
        chosen = _pick(video_ids, MAX_WEBP_VIDEOS)
        if not chosen:
            return None
        fetched = await asyncio.gather(*(_fetch_bytes(webp_url(v)) for v in chosen))
        sources = [b for b in fetched if b]
        if not sources:
            return None
        ok = await asyncio.to_thread(_build_webp, sources, out_path)
        return out_path if ok else None


async def build_collection_collage(share_id: str, video_ids) -> Optional[Path]:
    """Build (or return cached) the grid collage JPEG for a tag share."""
    out_path = SHARES_DIR / f"collection-{share_id}.jpg"
    if _is_fresh(out_path):
        return out_path
    async with _lock_for(f"jpg:{share_id}"):
        if _is_fresh(out_path):
            return out_path
        chosen = _pick(video_ids, MAX_COLLAGE_VIDEOS)
        if not chosen:
            return None
        fetched = await asyncio.gather(*(_fetch_bytes(screenshot_url(v)) for v in chosen))
        sources = [b for b in fetched if b]
        if not sources:
            return None
        ok = await asyncio.to_thread(_build_collage, sources, out_path)
        return out_path if ok else None
