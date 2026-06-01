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

# Keyed by (tag_id, respect_limit_tag). The respect flag MUST be part of the key:
# a tag can be shared both publicly (limit_to_tag applied -> filtered set) and as
# a password-protected share (limit_to_tag bypassed -> full set), and the two
# membership sets differ. Without the flag in the key, whichever request warmed
# the cache first would leak its set to the other — a public share could then
# serve un-curated videos (or a private share 404 valid ones).
_CacheKey = tuple[str, bool]
_tag_membership_cache: dict[_CacheKey, tuple[float, set[int]]] = {}
_tag_membership_lock = Lock()
# In-flight fetches, keyed identically, for single-flight coalescing (see below).
# Touched only from the event-loop thread, so it needs no separate lock.
_tag_membership_inflight: dict[_CacheKey, asyncio.Future] = {}


def _cache_key(tag_id: str, respect_limit_tag: bool) -> _CacheKey:
    return (str(tag_id), bool(respect_limit_tag))


def clear_tag_membership_cache() -> int:
    """Drop all cached tag->video-id sets. Returns the number of tags evicted."""
    with _tag_membership_lock:
        count = len(_tag_membership_cache)
        _tag_membership_cache.clear()
    logger.info(f"Cleared tag membership cache ({count} tag(s))")
    return count


async def _fetch_and_cache_tag_video_ids(tag_id: str, respect_limit_tag: bool = True) -> set[int]:
    """Fetch a tag's full video-id set from Stash and cache it.

    On a Stash error get_all_videos_by_tag yields an empty list; we deliberately
    do NOT cache empty results so a transient upstream hiccup can't poison the
    cache into 404-ing every video for the whole TTL window."""
    all_videos = await get_all_videos_by_tag(tag_id, respect_limit_tag=respect_limit_tag)
    ids = {int(v["id"]) for v in all_videos}
    if ids:
        with _tag_membership_lock:
            _tag_membership_cache[_cache_key(tag_id, respect_limit_tag)] = (
                time.time() + TAG_MEMBERSHIP_TTL_SECONDS, ids)
    return ids


async def get_tag_video_ids(tag_id: str, respect_limit_tag: bool = True) -> set[int]:
    """Return the set of Stash scene ids belonging to a tag, cached with TTL.

    ``respect_limit_tag=False`` returns the tag's full (unfiltered) membership,
    used by password-protected tag shares. The cache partitions on this flag so
    the filtered and unfiltered sets never clobber one another.
    """
    key = _cache_key(tag_id, respect_limit_tag)
    now = time.time()
    with _tag_membership_lock:
        entry = _tag_membership_cache.get(key)
        if entry and entry[0] > now:
            return entry[1]
    # Cache miss/expiry. Coalesce concurrent misses for the SAME key onto a single
    # fetch (single-flight) so a burst — e.g. many viewers hitting a TTL boundary
    # at once — doesn't fan out into N identical heavy Stash queries (a "stampede").
    # The fetch runs outside the cache lock, so misses for *other* keys aren't
    # blocked while we wait on Stash.
    inflight = _tag_membership_inflight.get(key)
    if inflight is not None:
        return await inflight
    inflight = asyncio.ensure_future(_fetch_and_cache_tag_video_ids(tag_id, respect_limit_tag))
    _tag_membership_inflight[key] = inflight
    try:
        return await inflight
    finally:
        _tag_membership_inflight.pop(key, None)


async def is_video_in_tag(tag_id: str, video_id: int, respect_limit_tag: bool = True) -> bool:
    """True if video_id belongs to the given Stash tag (TTL-cached).

    ``respect_limit_tag=False`` checks against the tag's full contents (for
    password-protected shares); the default checks the limit_to_tag-filtered set.
    """
    return int(video_id) in await get_tag_video_ids(tag_id, respect_limit_tag=respect_limit_tag)
