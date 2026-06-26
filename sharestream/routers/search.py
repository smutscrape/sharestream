"""Scoped search API + stale-while-revalidate entity autocomplete cache.

Search endpoints:
  GET /api/search?q=<query>&tags=<id1,id2>&performers=<p1,p2>&studios=<s1,s2>
       &sort=<mode>&gallery=<share_id>
    - Unscoped: returns scenes carrying the configured PUBLIC or LISTED
      visibility tags, matching the query string against title.
    - Scoped (?gallery=<share_id>): returns scenes belonging to that tag share's
      Stash tag, matching the query. Access is enforced via the tag share's
      password/expiry.
    - Optional ``tags``: comma-separated tag IDs; results filtered to videos
      carrying ALL specified tags (intersection).
    - Optional ``performers``: comma-separated performer IDs; results filtered
      to videos carrying ANY of the specified performers (union).
    - Optional ``studios``: comma-separated studio IDs; results filtered to
      videos from ANY of the specified studios (union).
    - Optional ``sort``: date, title, hits, rating, duration, random.

GET /api/tags/autocomplete?q=<query>&gallery=<share_id>
    - Returns ``[{"type": "tag"|"performer"|"studio", "id": "...", "name": "..."}]``
      of entities that exist within the gallery's Stash tag scope, filtered by
      ``q`` substring. Results are sorted by type (performers, studios, tags)
      then alphabetically within each group.
    - Backed by a stale-while-revalidate cache: stale data is returned
      instantly while a background task refreshes from Stash for the next
      request."""
from __future__ import annotations

import asyncio
import logging
import time
from threading import Lock
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from sharestream.backends.stash import (
    GRAPHQL_URL,
    _graphql_headers,
    get_videos_by_tag,
    http_client,
)
from sharestream.config import VISIBILITY_LISTED, VISIBILITY_PUBLIC
from sharestream.db.models import SharedTag
from sharestream.db.session import get_db
from sharestream.services.access import (
    ensure_not_expired,
    has_valid_pw_cookie,
    tag_share_respects_limit_tag,
)
from sharestream.services.galleries import format_duration, normalize_sort, parse_aspect, sort_video_dicts
from sharestream.services.hits import get_total_plays_map
from sharestream.services.slugs import canonical_video_slugs

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Stale-while-revalidate entity autocomplete cache
# ---------------------------------------------------------------------------
# Per-gallery cache: {gallery_share_id: {"data": [...], "expires": float,
# "updating": bool}}.  Each entry in ``data`` is a dict with ``type``
# ("tag"|"performer"|"studio"), ``id``, and ``name``.  A background asyncio
# task repopulates stale entries after returning the stale data to the caller
# immediately.

_ENTITY_VOCAB_CACHE: dict[str, dict] = {}
_ENTITY_VOCAB_LOCK = Lock()
_ENTITY_VOCAB_TTL = 600  # 10 minutes

# Sort order for entity types in the dropdown.
_ENTITY_TYPE_ORDER = {"performer": 0, "studio": 1, "tag": 2}


async def _fetch_and_update_cache(gallery_share_id: str, stash_tag_id: str):
    """Background task: fetch all tags, performers, and studios from Stash
    for a gallery's Stash tag scope and repopulate the entity cache."""
    query = {
        "operationName": "FindScenes",
        "variables": {
            "filter": {"page": 1, "per_page": 20000, "sort": "id", "direction": "ASC"},
            "scene_filter": {
                "tags": {
                    "value": [str(stash_tag_id)],
                    "modifier": "INCLUDES",
                    "depth": -1,
                }
            },
        },
        "query": (
            "query FindScenes($filter: FindFilterType, $scene_filter: SceneFilterType) {"
            "  findScenes(filter: $filter, scene_filter: $scene_filter) {"
            "    scenes {"
            "      tags { id name }"
            "      performers { id name }"
            "      studio { id name }"
            "    }"
            "  }"
            "}"
        ),
    }

    try:
        response = await http_client.post(
            GRAPHQL_URL, json=query, headers=_graphql_headers()
        )
        response.raise_for_status()
        data = response.json()

        if data.get("errors"):
            logger.error(
                "GraphQL errors fetching autocomplete entities for %s: %s",
                gallery_share_id, data["errors"],
            )
            with _ENTITY_VOCAB_LOCK:
                if gallery_share_id in _ENTITY_VOCAB_CACHE:
                    _ENTITY_VOCAB_CACHE[gallery_share_id]["updating"] = False
            return

        tags_map: dict[str, str] = {}
        performers_map: dict[str, str] = {}
        studios_map: dict[str, str] = {}

        for scene in (data.get("data") or {}).get("findScenes", {}).get("scenes", []):
            for tag in scene.get("tags", []):
                tags_map[tag["id"]] = tag["name"]
            for performer in scene.get("performers", []):
                performers_map[performer["id"]] = performer["name"]
            studio = scene.get("studio")
            if studio:
                studios_map[studio["id"]] = studio["name"]

        # Build entity list with type markers.
        entities: list[dict] = []
        for pid, name in performers_map.items():
            entities.append({"type": "performer", "id": pid, "name": name})
        for sid, name in studios_map.items():
            entities.append({"type": "studio", "id": sid, "name": name})
        for tid, name in tags_map.items():
            entities.append({"type": "tag", "id": tid, "name": name})

        # Sort by type order, then alphabetically within each group.
        entities.sort(
            key=lambda e: (_ENTITY_TYPE_ORDER.get(e["type"], 3), e["name"].lower()),
        )

        with _ENTITY_VOCAB_LOCK:
            _ENTITY_VOCAB_CACHE[gallery_share_id] = {
                "data": entities,
                "expires": time.time() + _ENTITY_VOCAB_TTL,
                "updating": False,
            }
        logger.info(
            "Entity cache updated for gallery %s (%d entities)",
            gallery_share_id, len(entities),
        )
    except Exception as e:
        logger.error(
            "Failed to fetch autocomplete entities for %s: %s",
            gallery_share_id, e,
        )
        with _ENTITY_VOCAB_LOCK:
            if gallery_share_id in _ENTITY_VOCAB_CACHE:
                _ENTITY_VOCAB_CACHE[gallery_share_id]["updating"] = False


@router.get("/api/tags/autocomplete")
async def entity_autocomplete(
    q: str = Query("", min_length=0),
    gallery: str | None = None,
    db: Session = Depends(get_db),
):
    """Return entities (tags, performers, studios) available within a gallery
    for faceted search autocomplete.

    Returns ``[{"type": "tag"|"performer"|"studio", "id": "123", "name": "..."}]``
    filtered by ``q`` substring (case-insensitive).  Backed by a
    stale-while-revalidate cache: stale data is returned instantly while a
    background task refreshes from Stash for the next request.  An empty/blank
    ``q`` returns the full entity list (still cached).

    Results are sorted by type (performers first, then studios, then tags)
    and alphabetically within each group."""
    if not gallery:
        return []

    tag_share = db.query(SharedTag).filter(SharedTag.share_id == gallery).first()
    if not tag_share:
        return []

    # We don't enforce password/expiry here — entity names are not sensitive
    # (they appear on every video card in the gallery).  But the gallery
    # must exist.

    now = time.time()
    trigger_update = False

    with _ENTITY_VOCAB_LOCK:
        cached = _ENTITY_VOCAB_CACHE.get(gallery)

        if not cached:
            # Cache miss: trigger background fetch, return empty for now.
            trigger_update = True
            cached = {"data": [], "expires": 0, "updating": True}
            _ENTITY_VOCAB_CACHE[gallery] = cached
        elif cached["expires"] < now and not cached.get("updating"):
            # Cache is stale and not currently updating: serve stale + refresh.
            trigger_update = True
            cached["updating"] = True

    if trigger_update:
        asyncio.create_task(_fetch_and_update_cache(gallery, tag_share.stash_tag_id))

    # Filter the results by the query string.
    q_lower = (q or "").strip().lower()
    all_entities = _ENTITY_VOCAB_CACHE.get(gallery, {}).get("data", [])
    if q_lower:
        matches = [e for e in all_entities if q_lower in e["name"].lower()]
    else:
        matches = list(all_entities)

    return matches[:20]


# ---------------------------------------------------------------------------
# Search endpoints
# ---------------------------------------------------------------------------

def _match_query(title: Optional[str], query: str) -> bool:
    """True if the scene's title contains the query substring (case-insensitive)."""
    if not title:
        return False
    return query.lower() in title.lower()


def _has_all_tags(video: dict, required_tag_ids: set[int]) -> bool:
    """True if the video carries ALL of the required tag IDs."""
    video_tag_ids = {int(t["id"]) for t in video.get("tags", [])}
    return required_tag_ids.issubset(video_tag_ids)


def _has_any_performer(video: dict, required_performer_ids: set[int]) -> bool:
    """True if the video carries ANY of the required performer IDs (union)."""
    if not required_performer_ids:
        return True
    video_performer_ids = {int(p["id"]) for p in video.get("performers", [])}
    return bool(required_performer_ids & video_performer_ids)


def _has_any_studio(video: dict, required_studio_ids: set[int]) -> bool:
    """True if the video's studio is one of the required studio IDs (union)."""
    if not required_studio_ids:
        return True
    studio = video.get("studio")
    if not studio:
        return False
    return int(studio.get("id", 0)) in required_studio_ids


@router.get("/api/search")
async def search(
    request: Request,
    q: str = "",
    gallery: str | None = None,
    tags: str | None = None,
    performers: str | None = None,
    studios: str | None = None,
    sort: str | None = None,
    db: Session = Depends(get_db),
):
    """Search videos, optionally scoped to a gallery (tag share).

    ``q`` is the search query (substring against scene title). An empty/blank
    query returns an empty result set when no filter is given.

    ``tags`` is a comma-separated list of Stash tag IDs; results are filtered
    to videos carrying ALL of the specified tags (intersection).

    ``performers`` is a comma-separated list of Stash performer IDs; results
    are filtered to videos carrying ANY of the specified performers (union).

    ``studios`` is a comma-separated list of Stash studio IDs; results are
    filtered to videos from ANY of the specified studios (union).

    ``sort`` is a sort mode (date, title, hits, rating, duration, random).

    When ``gallery`` is provided, the search is scoped to that tag share's
    Stash tag and access is enforced (expiry + password + membership). The
    returned ``share_url`` points at the gallery-scoped route
    ``/{gallery_slug}/{sqid}``.

    Without ``gallery``, the search is scoped to the configured PUBLIC/LISTED
    visibility tags; ``share_url`` points at the global ``/v/{sqid}`` route."""
    query = (q or "").strip()

    # Parse comma-separated entity IDs.
    def _parse_ids(raw: str | None) -> set[int]:
        ids: set[int] = set()
        if raw:
            for tok in raw.split(","):
                tok = tok.strip()
                if tok:
                    try:
                        ids.add(int(tok))
                    except ValueError:
                        pass  # skip invalid IDs silently
        return ids

    required_tag_ids = _parse_ids(tags)
    required_performer_ids = _parse_ids(performers)
    required_studio_ids = _parse_ids(studios)

    effective_sort = normalize_sort(sort) or "date"

    if not query and not required_tag_ids and not required_performer_ids and not required_studio_ids:
        return {
            "results": [], "query": q, "gallery": gallery,
            "tags": tags, "performers": performers, "studios": studios,
            "sort": effective_sort,
        }

    if gallery:
        return await _search_in_gallery(
            request, db, gallery, query,
            required_tag_ids, required_performer_ids, required_studio_ids,
            effective_sort,
        )

    return await _search_global(
        db, query, required_tag_ids, required_performer_ids, required_studio_ids,
        effective_sort,
    )


async def _search_in_gallery(
    request: Request,
    db: Session,
    share_id: str,
    query: str,
    required_tag_ids: set[int],
    required_performer_ids: set[int],
    required_studio_ids: set[int],
    sort: str,
):
    """Search within a specific tag share's collection."""
    tag_share = db.query(SharedTag).filter(SharedTag.share_id == share_id).first()
    if not tag_share:
        raise HTTPException(status_code=404, detail="Gallery not found")

    ensure_not_expired(tag_share.expires_at, "Gallery has expired")
    if tag_share.password_hash and not has_valid_pw_cookie(request, share_id):
        raise HTTPException(status_code=403, detail="Password required")

    respect_limit = tag_share_respects_limit_tag(
        tag_share.password_hash, tag_share.show_in_gallery,
        tag_share.apply_limit_tag,
    )

    all_videos, _ = await get_videos_by_tag(
        tag_share.stash_tag_id, respect_limit_tag=respect_limit,
    )

    # Apply text query filter
    if query:
        all_videos = [v for v in all_videos if _match_query(v.get("title"), query)]

    # Apply tag intersection filter
    if required_tag_ids:
        all_videos = [v for v in all_videos if _has_all_tags(v, required_tag_ids)]

    # Apply performer union filter
    if required_performer_ids:
        all_videos = [v for v in all_videos if _has_any_performer(v, required_performer_ids)]

    # Apply studio union filter
    if required_studio_ids:
        all_videos = [v for v in all_videos if _has_any_studio(v, required_studio_ids)]

    if not all_videos:
        return {
            "results": [], "query": query, "gallery": share_id,
            "tags": ",".join(str(t) for t in required_tag_ids) if required_tag_ids else None,
            "performers": ",".join(str(p) for p in required_performer_ids) if required_performer_ids else None,
            "studios": ",".join(str(s) for s in required_studio_ids) if required_studio_ids else None,
            "sort": sort,
        }

    # Aggregate play counts
    vid_ids = [int(v["id"]) for v in all_videos]
    total_plays = get_total_plays_map(db, vid_ids)
    for v in all_videos:
        v["hits"] = total_plays.get(int(v["id"]), 0)

    sort_video_dicts(all_videos, sort)

    all_videos = all_videos[:200]

    slug_map = canonical_video_slugs(db, [int(v["id"]) for v in all_videos])
    results = []
    for v in all_videos:
        vid = int(v["id"])
        sqid = slug_map.get(vid, str(vid))
        results.append({
            "stash_video_id": vid,
            "video_name": v.get("title") or "Untitled",
            "sqid": sqid,
            "share_url": f"/{share_id}/{sqid}",
            "preview_url": f"/media/{sqid}/webp",
            "thumbnail_url": f"/media/{sqid}/thumbnail.jpg",
            "lazy_thumbnail_url": None,
            "hits": v.get("hits", 0),
            "duration": v.get("duration"),
            "duration_label": format_duration(v.get("duration")),
            "resolution": v.get("resolution"),
            "aspect": parse_aspect(v.get("resolution")),
        })
    return {
        "results": results, "query": query, "gallery": share_id,
        "tags": ",".join(str(t) for t in required_tag_ids) if required_tag_ids else None,
        "performers": ",".join(str(p) for p in required_performer_ids) if required_performer_ids else None,
        "studios": ",".join(str(s) for s in required_studio_ids) if required_studio_ids else None,
        "sort": sort, "total": len(results),
    }


async def _search_global(
    db: Session,
    query: str,
    required_tag_ids: set[int],
    required_performer_ids: set[int],
    required_studio_ids: set[int],
    sort: str,
):
    """Search across all public/listed scenes."""
    seen: set[int] = set()
    matched: list[dict] = []

    tag_ids = []
    if VISIBILITY_PUBLIC:
        tag_ids.append(str(VISIBILITY_PUBLIC))
    if VISIBILITY_LISTED:
        tag_ids.append(str(VISIBILITY_LISTED))

    for tag_id in tag_ids:
        videos, _ = await get_videos_by_tag(tag_id, respect_limit_tag=False)
        for v in videos:
            vid = int(v["id"])
            if vid in seen:
                continue
            if query and not _match_query(v.get("title"), query):
                continue
            if required_tag_ids and not _has_all_tags(v, required_tag_ids):
                continue
            if required_performer_ids and not _has_any_performer(v, required_performer_ids):
                continue
            if required_studio_ids and not _has_any_studio(v, required_studio_ids):
                continue
            seen.add(vid)
            matched.append(v)

    if not matched:
        return {
            "results": [], "query": query, "gallery": None,
            "tags": ",".join(str(t) for t in required_tag_ids) if required_tag_ids else None,
            "performers": ",".join(str(p) for p in required_performer_ids) if required_performer_ids else None,
            "studios": ",".join(str(s) for s in required_studio_ids) if required_studio_ids else None,
            "sort": sort,
        }

    vid_ids = [int(v["id"]) for v in matched]
    total_plays = get_total_plays_map(db, vid_ids)
    for v in matched:
        v["hits"] = total_plays.get(int(v["id"]), 0)

    sort_video_dicts(matched, sort)
    matched = matched[:200]

    slug_map = canonical_video_slugs(db, [int(v["id"]) for v in matched])
    results = []
    for v in matched:
        vid = int(v["id"])
        sqid = slug_map.get(vid, str(vid))
        results.append({
            "stash_video_id": vid,
            "video_name": v.get("title") or "Untitled",
            "sqid": sqid,
            "share_url": f"/v/{sqid}",
            "preview_url": f"/media/{sqid}/webp",
            "thumbnail_url": f"/media/{sqid}/thumbnail.jpg",
            "lazy_thumbnail_url": None,
            "hits": v.get("hits", 0),
            "duration": v.get("duration"),
            "duration_label": format_duration(v.get("duration")),
            "resolution": v.get("resolution"),
            "aspect": parse_aspect(v.get("resolution")),
        })
    return {
        "results": results, "query": query, "gallery": None,
        "tags": ",".join(str(t) for t in required_tag_ids) if required_tag_ids else None,
        "performers": ",".join(str(p) for p in required_performer_ids) if required_performer_ids else None,
        "studios": ",".join(str(s) for s in required_studio_ids) if required_studio_ids else None,
        "sort": sort, "total": len(results),
    }
