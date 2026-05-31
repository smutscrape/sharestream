"""Tag membership cache.

Media endpoints (thumbnails, HLS segments, previews, mp4/webp) must verify that
a requested video id actually belongs to a shared tag before proxying it from
Stash; otherwise a valid tag share id could be walked to pull arbitrary scenes.
Doing that verification by re-fetching the tag's full scene list from Stash on
EVERY media hit hammers Stash's SQLite under concurrent load, so we cache just
the set of scene ids per tag with a TTL (``TAG_MEMBERSHIP_TTL_SECONDS``).

Multi-worker note: this cache is per-process and in-memory. The lookup API
(:func:`get_tag_video_ids` / :func:`is_video_in_tag`) is intentionally narrow so
a shared-cache (e.g. Redis) implementation can be slotted in behind it later
without touching callers. Do NOT scatter cache state outside this module.
"""
from __future__ import annotations

import asyncio
import logging
import time
from threading import Lock

from sharestream.backends.stash import get_all_videos_by_tag
from sharestream.config import TAG_MEMBERSHIP_TTL_SECONDS

logger = logging.getLogger(__name__)

_tag_membership_cache: dict[str, tuple[float, set[int]]] = {}
_tag_membership_lock = Lock()
# In-flight fetches, keyed by tag id, for single-flight coalescing (see below).
# Touched only from the event-loop thread, so it needs no separate lock.
_tag_membership_inflight: dict[str, asyncio.Future] = {}


def clear_tag_membership_cache() -> int:
    """Drop all cached tag->video-id sets. Returns the number of tags evicted."""
    with _tag_membership_lock:
        count = len(_tag_membership_cache)
        _tag_membership_cache.clear()
    logger.info(f"Cleared tag membership cache ({count} tag(s))")
    return count


async def _fetch_and_cache_tag_video_ids(tag_id: str) -> set[int]:
    """Fetch a tag's full video-id set from Stash and cache it.

    On a Stash error get_all_videos_by_tag yields an empty list; we deliberately
    do NOT cache empty results so a transient upstream hiccup can't poison the
    cache into 404-ing every video for the whole TTL window."""
    all_videos = await get_all_videos_by_tag(tag_id)
    ids = {int(v["id"]) for v in all_videos}
    if ids:
        with _tag_membership_lock:
            _tag_membership_cache[str(tag_id)] = (time.time() + TAG_MEMBERSHIP_TTL_SECONDS, ids)
    return ids


async def get_tag_video_ids(tag_id: str) -> set[int]:
    """Return the set of Stash scene ids belonging to a tag, cached with TTL."""
    key = str(tag_id)
    now = time.time()
    with _tag_membership_lock:
        entry = _tag_membership_cache.get(key)
        if entry and entry[0] > now:
            return entry[1]
    # Cache miss/expiry. Coalesce concurrent misses for the SAME tag onto a single
    # fetch (single-flight) so a burst — e.g. many viewers hitting a TTL boundary
    # at once — doesn't fan out into N identical heavy Stash queries (a "stampede").
    # The fetch runs outside the cache lock, so misses for *other* tags aren't
    # blocked while we wait on Stash.
    inflight = _tag_membership_inflight.get(key)
    if inflight is not None:
        return await inflight
    inflight = asyncio.ensure_future(_fetch_and_cache_tag_video_ids(tag_id))
    _tag_membership_inflight[key] = inflight
    try:
        return await inflight
    finally:
        _tag_membership_inflight.pop(key, None)


async def is_video_in_tag(tag_id: str, video_id: int) -> bool:
    """True if video_id belongs to the given Stash tag (TTL-cached)."""
    return int(video_id) in await get_tag_video_ids(tag_id)
