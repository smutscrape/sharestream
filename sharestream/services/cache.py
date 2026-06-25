"""Tag membership cache.

Media endpoints (thumbnails, HLS segments, previews, mp4/webp) must verify that
a requested video id actually belongs to a shared tag before proxying it from
Stash; otherwise a valid tag share id could be walked to pull arbitrary scenes.
Re-querying Stash on EVERY media hit would hammer it, so memberships are cached
with a TTL (``TAG_MEMBERSHIP_TTL_SECONDS``).

Two complementary caches back :func:`is_video_in_tag`:

* a **per-tag id set** (:func:`get_tag_video_ids`), ideal when something needs to
  test many videos against one tag (a gallery's worth of thumbnails) — one fetch
  answers them all; and
* a **per-(tag, video) boolean** for lone hits (a direct tag-video link, an HLS
  segment storm for a single video, a social-embed crawler), verified with a
  cheap single-scene Stash probe so we never list an entire (possibly huge) tag
  just to check one video.

:func:`is_video_in_tag` prefers an already-cached id set, then the per-video
cache, then the cheap probe (falling back to the full id set only if the probe
errors). Galleries that have already fetched a tag's full contents can
:func:`prime_tag_membership` to seed the id set so their thumbnails hit warm.

Multi-worker note: these caches are per-process and in-memory. The lookup API is
intentionally narrow so a shared-cache (e.g. Redis) implementation can be slotted
in behind it later without touching callers. Do NOT scatter cache state outside
this module.
"""
from __future__ import annotations

import asyncio
import logging
import time
from threading import Lock

from sharestream.backends.stash import (
    get_scene_tag_ids as _fetch_scene_tag_ids,
    get_tag_scene_ids,
    tag_contains_scene,
)
from sharestream.config import TAG_MEMBERSHIP_TTL_SECONDS

logger = logging.getLogger(__name__)

# Both caches key on (tag_id, respect_limit_tag). The respect flag MUST be part
# of the key: the same tag can be exposed with limit_to_tag applied (filtered set,
# e.g. a featured public share) and with it bypassed (full set, e.g. a password-
# protected or non-featured share), and the two membership answers differ.
# Without the flag in the key, whichever request warmed the cache first would leak
# its answer to the other — a limited share could serve un-curated videos (or a
# full-access share 404 valid ones).
_SetKey = tuple[str, bool]
_SceneKey = tuple[str, int, bool]

_tag_set_cache: dict[_SetKey, tuple[float, set[int]]] = {}
_scene_cache: dict[_SceneKey, tuple[float, bool]] = {}
_cache_lock = Lock()

# In-flight fetches for single-flight coalescing. Touched only from the
# event-loop thread, so they need no separate lock.
_tag_set_inflight: dict[_SetKey, asyncio.Future] = {}
_scene_inflight: dict[_SceneKey, asyncio.Future] = {}


def _set_key(tag_id: str, respect_limit_tag: bool) -> _SetKey:
    return (str(tag_id), bool(respect_limit_tag))


def _scene_key(tag_id: str, video_id: int, respect_limit_tag: bool) -> _SceneKey:
    return (str(tag_id), int(video_id), bool(respect_limit_tag))


def clear_tag_membership_cache() -> int:
    """Drop all cached memberships (both the id-set and per-video caches).

    Returns the number of tag-id-set entries evicted.
    """
    with _cache_lock:
        count = len(_tag_set_cache)
        _tag_set_cache.clear()
        _scene_cache.clear()
    logger.info(f"Cleared tag membership cache ({count} id-set(s))")
    return count


def prime_tag_membership(tag_id: str, video_ids, respect_limit_tag: bool = True) -> None:
    """Seed the id-set cache from a tag listing a caller already fetched.

    A gallery that has loaded a tag's *complete* contents for display can call
    this so the page's own thumbnail sub-requests find the set warm instead of
    each probing Stash. Pass only a COMPLETE set — priming a partial page would
    make :func:`is_video_in_tag` wrongly 404 the videos it omits.
    """
    ids = {int(v) for v in video_ids}
    if not ids:
        return
    with _cache_lock:
        _tag_set_cache[_set_key(tag_id, respect_limit_tag)] = (
            time.time() + TAG_MEMBERSHIP_TTL_SECONDS, ids)


async def _fetch_and_cache_tag_video_ids(tag_id: str, respect_limit_tag: bool = True) -> set[int]:
    """Fetch a tag's scene-id set from Stash (id-only) and cache it.

    On a Stash error get_tag_scene_ids yields an empty set; we deliberately do
    NOT cache empty results so a transient upstream hiccup can't poison the cache
    into 404-ing every video for the whole TTL window."""
    ids = await get_tag_scene_ids(tag_id, respect_limit_tag=respect_limit_tag)
    if ids:
        with _cache_lock:
            _tag_set_cache[_set_key(tag_id, respect_limit_tag)] = (
                time.time() + TAG_MEMBERSHIP_TTL_SECONDS, ids)
    return ids


async def get_tag_video_ids(tag_id: str, respect_limit_tag: bool = True) -> set[int]:
    """Return the set of Stash scene ids belonging to a tag, cached with TTL.

    ``respect_limit_tag=False`` returns the tag's full (unfiltered) membership,
    used by password-protected or non-featured tag shares. The cache partitions
    on this flag so the filtered and unfiltered sets never clobber one another.
    """
    key = _set_key(tag_id, respect_limit_tag)
    now = time.time()
    with _cache_lock:
        entry = _tag_set_cache.get(key)
        if entry and entry[0] > now:
            return entry[1]
    # Cache miss/expiry. Coalesce concurrent misses for the SAME key onto a single
    # fetch (single-flight) so a burst — e.g. many viewers hitting a TTL boundary
    # at once — doesn't fan out into N identical heavy Stash queries (a "stampede").
    inflight = _tag_set_inflight.get(key)
    if inflight is not None:
        return await inflight
    inflight = asyncio.ensure_future(_fetch_and_cache_tag_video_ids(tag_id, respect_limit_tag))
    _tag_set_inflight[key] = inflight
    try:
        return await inflight
    finally:
        _tag_set_inflight.pop(key, None)


async def _probe_and_cache_scene(tag_id: str, video_id: int, respect_limit_tag: bool) -> bool:
    """Resolve one video's membership cheaply, caching the boolean.

    Tries the single-scene Stash probe first; if that errors (returns None) we
    fall back to the full id set so correctness never depends on the probe being
    supported by the running Stash version.
    """
    present = await tag_contains_scene(tag_id, video_id, respect_limit_tag=respect_limit_tag)
    if present is None:
        ids = await get_tag_video_ids(tag_id, respect_limit_tag=respect_limit_tag)
        return int(video_id) in ids
    # Only cache a definitive probe result. (A True/False from Stash is
    # authoritative; the None path above already consulted the id set.)
    with _cache_lock:
        _scene_cache[_scene_key(tag_id, video_id, respect_limit_tag)] = (
            time.time() + TAG_MEMBERSHIP_TTL_SECONDS, present)
    return present


async def is_video_in_tag(tag_id: str, video_id: int, respect_limit_tag: bool = True) -> bool:
    """True if video_id belongs to the given Stash tag (TTL-cached).

    ``respect_limit_tag=False`` checks against the tag's full contents (for
    password-protected or non-featured shares); the default checks the
    limit_to_tag-filtered set.
    """
    vid = int(video_id)
    now = time.time()
    with _cache_lock:
        # Prefer an already-cached full set (e.g. primed by a gallery render).
        set_entry = _tag_set_cache.get(_set_key(tag_id, respect_limit_tag))
        if set_entry and set_entry[0] > now:
            return vid in set_entry[1]
        scene_entry = _scene_cache.get(_scene_key(tag_id, vid, respect_limit_tag))
        if scene_entry and scene_entry[0] > now:
            return scene_entry[1]
    # Miss: resolve via the cheap probe (single-flight per (tag, video, respect)).
    skey = _scene_key(tag_id, vid, respect_limit_tag)
    inflight = _scene_inflight.get(skey)
    if inflight is not None:
        return await inflight
    inflight = asyncio.ensure_future(_probe_and_cache_scene(tag_id, vid, respect_limit_tag))
    _scene_inflight[skey] = inflight
    try:
        return await inflight
    finally:
        _scene_inflight.pop(skey, None)


# ------------------------------------------------------------------
# Per-scene tag-id set (visibility resolver hot path)
# ------------------------------------------------------------------
# The visibility resolver and the media gate ask "which tags does scene X carry"
# on every /v/ render and every media sub-request (including each HLS segment).
# Caching the scene's full tag-id set with the same TTL + single-flight discipline
# means a segment storm for one video coalesces to a single Stash fetch, and the
# resolver answers public/listed/hidden/unlisted from the cached set without
# re-querying. Keyed by scene id alone (a scene's tags don't vary by requester).
_scene_tags_cache: dict[int, tuple[float, set[str]]] = {}
_scene_tags_inflight: dict[int, asyncio.Future] = {}


def clear_scene_tag_cache() -> int:
    """Drop all cached per-scene tag sets. Returns the number of entries evicted.
    Call after a scene's tags change in Stash if an immediate refresh is needed
    (otherwise entries expire after TAG_MEMBERSHIP_TTL_SECONDS)."""
    with _cache_lock:
        count = len(_scene_tags_cache)
        _scene_tags_cache.clear()
    logger.info(f"Cleared scene tag cache ({count} scene(s))")
    return count


async def _fetch_and_cache_scene_tags(scene_id: int) -> set[str]:
    """Fetch a scene's tag-id set from Stash and cache it. On an upstream error
    (None) we return an empty set WITHOUT caching, so a transient hiccup can't
    pin a wrong (empty) answer for the whole TTL — the resolver treats 'no tags'
    as unlisted-but-allowed, so not caching keeps the next request authoritative."""
    ids = await _fetch_scene_tag_ids(scene_id)
    if ids is None:
        return set()
    with _cache_lock:
        _scene_tags_cache[int(scene_id)] = (time.time() + TAG_MEMBERSHIP_TTL_SECONDS, ids)
    return ids


async def get_scene_tag_ids(scene_id: int) -> set[str]:
    """Return the set of Stash tag ids (as strings) a scene carries, TTL-cached
    with single-flight coalescing. The unfiltered set (includes internal/limit
    tags) — the visibility resolver tests it against the configured tag ids."""
    sid = int(scene_id)
    now = time.time()
    with _cache_lock:
        entry = _scene_tags_cache.get(sid)
        if entry and entry[0] > now:
            return entry[1]
    inflight = _scene_tags_inflight.get(sid)
    if inflight is not None:
        return await inflight
    inflight = asyncio.ensure_future(_fetch_and_cache_scene_tags(sid))
    _scene_tags_inflight[sid] = inflight
    try:
        return await inflight
    finally:
        _scene_tags_inflight.pop(sid, None)
