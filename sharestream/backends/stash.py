"""Stash backend integration: all GraphQL queries and Stash media URL builders.

This is the single place that knows how to talk to Stash. Routers and services
call these helpers; no GraphQL or Stash URL construction should live elsewhere.

The ``limit_to_tag`` config (when set) is applied here in the scene-listing
queries so that every tag-listing path only ever sees videos carrying that tag.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException

from sharestream.config import LIMIT_TO_TAG, STASH_API_KEY, STASH_SERVER
from sharestream.core.http_client import http_client

logger = logging.getLogger(__name__)

GRAPHQL_URL = f"{STASH_SERVER}/graphql"


def _graphql_headers() -> dict:
    return {"ApiKey": STASH_API_KEY, "Content-Type": "application/json"}


# ------------------------------------------------------------------
# Stash media URL builders
# ------------------------------------------------------------------
def screenshot_url(stash_video_id: int) -> str:
    return f"{STASH_SERVER}/scene/{stash_video_id}/screenshot?apikey={STASH_API_KEY}"


def preview_url(stash_video_id: int) -> str:
    return f"{STASH_SERVER}/scene/{stash_video_id}/preview?apikey={STASH_API_KEY}"


def webp_url(stash_video_id: int) -> str:
    return f"{STASH_SERVER}/scene/{stash_video_id}/webp?apikey={STASH_API_KEY}"


def stream_url(stash_video_id: int, resolution: str) -> str:
    return f"{STASH_SERVER}/scene/{stash_video_id}/stream?apikey={STASH_API_KEY}&resolution={resolution}"


def playlist_url(stash_video_id: int, resolution: str) -> str:
    return f"{STASH_SERVER}/scene/{stash_video_id}/stream.m3u8?apikey={STASH_API_KEY}&resolution={resolution}"


def segment_url(stash_video_id: int, segment: str, resolution: str) -> str:
    return f"{STASH_SERVER}/scene/{stash_video_id}/stream.m3u8/{segment}?apikey={STASH_API_KEY}&resolution={resolution}"


# ------------------------------------------------------------------
# GraphQL queries
# ------------------------------------------------------------------
async def get_scene_meta(video_ids: list[int]) -> dict[int, dict]:
    """Batch-fetch {id: {rating, date, created_at}} for the given scene IDs."""
    if not video_ids:
        return {}

    query = {
        "operationName": "FindScenes",
        "variables": {"scene_ids": video_ids},
        "query": """
            query FindScenes($scene_ids: [Int!]) {
                findScenes(scene_ids: $scene_ids) {
                    scenes {
                        id
                        rating100
                        date
                        created_at
                    }
                }
            }
        """
    }

    try:
        response = await http_client.post(GRAPHQL_URL, json=query, headers=_graphql_headers())
        response.raise_for_status()
        data = response.json()

        if data.get("errors"):
            logger.error(f"GraphQL error getting scene meta: {data['errors']}")
            return {}

        scenes = data.get("data", {}).get("findScenes", {}).get("scenes", [])
        return {
            int(scene["id"]): {
                "rating": scene.get("rating100"),
                "date": scene.get("date"),
                "created_at": scene.get("created_at"),
            }
            for scene in scenes
        }
    except Exception as e:
        logger.error(f"Error fetching scene meta: {e}")
        return {}


async def find_tag_by_name(tag_name: str) -> dict | None:
    """Find a tag by name and return its ID and details"""
    query = {
        "operationName": "FindTags",
        "variables": {
            "filter": {
                "q": tag_name,
                "page": 1,
                "per_page": 1000,
                "sort": "scenes_count",
                "direction": "DESC"
            },
            "tag_filter": {}
        },
        "query": """
            query FindTags($filter: FindFilterType, $tag_filter: TagFilterType) {
                findTags(filter: $filter, tag_filter: $tag_filter) {
                    count
                    tags {
                        id
                        name
                        scene_count
                        __typename
                    }
                    __typename
                }
            }
        """
    }

    try:
        logger.debug(f"Searching for tag: {tag_name}")
        response = await http_client.post(GRAPHQL_URL, json=query, headers=_graphql_headers())
        response.raise_for_status()
        data = response.json()

        if data.get("errors"):
            logger.error(f"GraphQL error finding tag '{tag_name}': {data['errors']}")
            return None

        tags = data.get("data", {}).get("findTags", {}).get("tags", [])
        logger.debug(f"Found {len(tags)} tags matching '{tag_name}'")

        # Look for exact match first, then partial match
        for tag in tags:
            if tag["name"].lower() == tag_name.lower():
                logger.info(f"Exact match found for tag '{tag_name}': ID {tag['id']}")
                return tag

        # If no exact match, return first result if any
        if tags:
            logger.info(f"Partial match found for tag '{tag_name}': {tags[0]['name']} (ID {tags[0]['id']})")
            return tags[0]

        logger.warning(f"No tags found matching '{tag_name}'")
        return None

    except Exception as e:
        logger.error(f"Error finding tag '{tag_name}': {e}")
        return None


async def get_videos_by_tag(tag_id: str, page: int = 1, per_page: int = 1000, sort_by: str = 'date',
                            respect_limit_tag: bool = True) -> tuple[list, int]:
    """Get videos that have a specific tag - returns (videos, total_count).

    ``respect_limit_tag`` (default True) controls whether the global
    ``limit_to_tag`` safety filter is applied. Password-protected tag shares
    pass ``respect_limit_tag=False`` so a vetted, password-gated share can reach
    the tag's full contents while public shares stay limited to the approved
    tag. When ``limit_to_tag`` is unset this argument has no effect.
    """
    # Compose tag filter
    apply_limit = bool(LIMIT_TO_TAG) and respect_limit_tag
    tag_values = [tag_id]
    if apply_limit:
        tag_values = [str(LIMIT_TO_TAG), str(tag_id)]
    query = {
        "operationName": "FindScenes",
        "variables": {
            "filter": {
                "q": "",
                "page": page,
                "per_page": per_page,
                "sort": sort_by,
                "direction": "DESC"
            },
            "scene_filter": {
                "tags": {
                    "value": tag_values,
                    "excludes": [],
                    "modifier": "INCLUDES_ALL",
                    "depth": 0 if apply_limit else -1
                }
            }
        },
        "query": """
            query FindScenes($filter: FindFilterType, $scene_filter: SceneFilterType, $scene_ids: [Int!]) {
                findScenes(filter: $filter, scene_filter: $scene_filter, scene_ids: $scene_ids) {
                    count
                    scenes {
                        id
                        title
                        details
                        rating100
                        date
                        created_at
                        paths {
                            screenshot
                            preview
                            __typename
                        }
                        tags {
                            id
                            name
                            __typename
                        }
                        performers {
                            id
                            name
                            __typename
                        }
                        studio {
                            id
                            name
                            __typename
                        }
                        files {
                            width
                            height
                            __typename
                        }
                        __typename
                    }
                    __typename
                }
            }
        """
    }

    try:
        logger.debug(f"Getting videos for tag ID: {tag_id}, page: {page}, per_page: {per_page}")
        response = await http_client.post(GRAPHQL_URL, json=query, headers=_graphql_headers())
        response.raise_for_status()
        data = response.json()

        if data.get("errors"):
            logger.error(f"GraphQL error getting videos for tag {tag_id}: {data['errors']}")
            return [], 0

        result = data.get("data", {}).get("findScenes", {})
        scenes = result.get("scenes", [])
        total_count = result.get("count", 0)

        # Transform the data to a simpler format
        videos = []
        for scene in scenes:
            video = {
                "id": scene["id"],
                "title": scene["title"],
                "details": scene.get("details", ""),
                "rating": scene.get("rating100"),
                "date": scene.get("date"),
                "created_at": scene.get("created_at"),
                "screenshot": scene["paths"]["screenshot"],
                "preview": scene["paths"]["preview"],
                "tags": [{"id": tag["id"], "name": tag["name"]} for tag in scene.get("tags", [])
                         if not (LIMIT_TO_TAG and str(tag.get("id")) == str(LIMIT_TO_TAG))],
                "performers": [{"id": p["id"], "name": p["name"]} for p in scene.get("performers", [])],
                "studio": scene.get("studio", {}).get("name", "") if scene.get("studio") else "",
                "resolution": (
                    f"{scene['files'][0]['width']}x{scene['files'][0]['height']}"
                    if scene.get("files") and scene["files"][0].get("width") and scene["files"][0].get("height")
                    else None
                ),
            }
            videos.append(video)

        logger.info(f"Found {len(videos)} videos (total: {total_count}) for tag {tag_id}")
        return videos, total_count

    except Exception as e:
        logger.error(f"Error getting videos for tag {tag_id}: {e}")
        return [], 0


async def get_all_videos_by_tag(tag_id: str, respect_limit_tag: bool = True) -> list:
    """Helper to get all videos for a tag, handling pagination.

    ``respect_limit_tag`` is forwarded to :func:`get_videos_by_tag` (see there).
    """
    all_videos = []
    page = 1
    per_page = 1000
    while True:
        videos, total_count = await get_videos_by_tag(tag_id, page=page, per_page=per_page,
                                                       respect_limit_tag=respect_limit_tag)
        if not videos:
            break
        all_videos.extend(videos)
        if len(all_videos) >= total_count or total_count == 0:
            break
        page += 1
    logger.info(f"Fetched {len(all_videos)} total videos for tag_id {tag_id}")
    return all_videos


async def get_tag_scene_ids(tag_id: str, respect_limit_tag: bool = True) -> set[int]:
    """Return just the set of scene IDs in a tag (id-only GraphQL), paginated.

    Far cheaper than :func:`get_all_videos_by_tag`, which pulls full scene
    objects (title, tags, performers, files, …) for every scene. Membership
    checks only need IDs, so this is what the membership cache uses — an 11k-scene
    tag is a few small pages instead of a multi-megabyte payload.
    ``respect_limit_tag`` mirrors :func:`get_videos_by_tag`.
    """
    apply_limit = bool(LIMIT_TO_TAG) and respect_limit_tag
    tag_values = [str(tag_id)]
    if apply_limit:
        tag_values = [str(LIMIT_TO_TAG), str(tag_id)]
    query_str = """
        query FindScenes($filter: FindFilterType, $scene_filter: SceneFilterType) {
            findScenes(filter: $filter, scene_filter: $scene_filter) {
                count
                scenes { id }
            }
        }
    """
    ids: set[int] = set()
    page = 1
    per_page = 5000
    while True:
        query = {
            "operationName": "FindScenes",
            "variables": {
                "filter": {"q": "", "page": page, "per_page": per_page, "sort": "id", "direction": "ASC"},
                "scene_filter": {
                    "tags": {
                        "value": tag_values,
                        "excludes": [],
                        "modifier": "INCLUDES_ALL",
                        "depth": 0 if apply_limit else -1,
                    }
                },
            },
            "query": query_str,
        }
        try:
            response = await http_client.post(GRAPHQL_URL, json=query, headers=_graphql_headers())
            response.raise_for_status()
            data = response.json()
            if data.get("errors"):
                logger.error(f"GraphQL error getting scene ids for tag {tag_id}: {data['errors']}")
                break
            result = data.get("data", {}).get("findScenes", {})
            scenes = result.get("scenes", [])
            total_count = result.get("count", 0)
            if not scenes:
                break
            ids.update(int(s["id"]) for s in scenes)
            if len(ids) >= total_count or total_count == 0:
                break
            page += 1
        except Exception as e:
            logger.error(f"Error getting scene ids for tag {tag_id}: {e}")
            break
    logger.info(f"Fetched {len(ids)} scene ids for tag_id {tag_id} (respect_limit_tag={respect_limit_tag})")
    return ids


async def tag_contains_scene(tag_id: str, video_id: int, respect_limit_tag: bool = True) -> bool | None:
    """Cheap single-scene membership probe: does scene ``video_id`` carry ``tag_id``
    (and ``limit_to_tag`` when respected)?

    Returns True/False, or ``None`` on any upstream error/uncertainty so callers
    can fall back to the full id set. This lets a lone media hit (e.g. a direct
    tag-video link or a social-embed crawler) verify membership with one tiny,
    indexed Stash query instead of listing the entire tag.
    """
    apply_limit = bool(LIMIT_TO_TAG) and respect_limit_tag
    tag_values = [str(tag_id)]
    if apply_limit:
        tag_values = [str(LIMIT_TO_TAG), str(tag_id)]
    query = {
        "operationName": "FindScenes",
        "variables": {
            "filter": {"page": 1, "per_page": 1},
            "scene_filter": {
                "id": {"modifier": "EQUALS", "value": int(video_id)},
                "tags": {
                    "value": tag_values,
                    "excludes": [],
                    "modifier": "INCLUDES_ALL",
                    "depth": 0 if apply_limit else -1,
                },
            },
        },
        "query": """
            query FindScenes($filter: FindFilterType, $scene_filter: SceneFilterType) {
                findScenes(filter: $filter, scene_filter: $scene_filter) { count }
            }
        """,
    }
    try:
        response = await http_client.post(GRAPHQL_URL, json=query, headers=_graphql_headers())
        response.raise_for_status()
        data = response.json()
        if data.get("errors"):
            logger.error(f"GraphQL error probing scene {video_id} in tag {tag_id}: {data['errors']}")
            return None
        count = data.get("data", {}).get("findScenes", {}).get("count", 0)
        return count > 0
    except Exception as e:
        logger.error(f"Error probing scene {video_id} in tag {tag_id}: {e}")
        return None


async def get_videos_by_tag_name(tag_name: str, page: int = 1, per_page: int = 1000) -> tuple[list, dict | None]:
    """Get videos by tag name - returns (videos, tag_info)"""
    logger.debug(f"Getting videos by tag name: {tag_name}")

    tag_info = await find_tag_by_name(tag_name)
    if not tag_info:
        logger.warning(f"Tag '{tag_name}' not found")
        return [], None

    videos, total_count = await get_videos_by_tag(tag_info["id"], page, per_page)
    logger.info(f"Retrieved {len(videos)} videos for tag '{tag_name}' (ID: {tag_info['id']})")
    return videos, tag_info


async def fetch_scene_title(stash_id: int) -> dict:
    """Return ``{"title": ...}`` for a scene, raising HTTPException on failure.

    Mirrors the original ``/get_video_title`` behavior: 503 when Stash is
    unreachable, 404 when the scene/title is missing, 500 on GraphQL errors.
    """
    query = {
        "query": """
            query FindScene($id: ID!) {
                findScene(id: $id) {
                    title
                    files {
                        basename
                    }
                }
            }
        """,
        "variables": {"id": stash_id}
    }

    logger.debug(f"Querying Stash for title of scene ID: {stash_id}")
    try:
        response = await http_client.post(GRAPHQL_URL, json=query, headers=_graphql_headers())
        response.raise_for_status()
        data = response.json()

        if data.get("errors"):
            logger.error(f"GraphQL error from Stash: {data['errors']}")
            raise HTTPException(status_code=500, detail="GraphQL error from Stash")

        scene_data = data.get("data", {}).get("findScene")
        if scene_data and scene_data.get("title"):
            logger.info(f"Found title for Stash ID {stash_id}: {scene_data['title']}")
            return {"title": scene_data["title"]}
        else:
            logger.warning(f"Scene not found or title missing for Stash ID: {stash_id}")
            raise HTTPException(status_code=404, detail="Scene not found in Stash")
    except HTTPException:
        raise
    except httpx.RequestError as e:
        logger.error(f"Error connecting to Stash GraphQL API: {e}")
        raise HTTPException(status_code=503, detail="Could not connect to Stash API")
    except Exception as e:
        logger.error(f"Error fetching video title for ID {stash_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error fetching video title")


async def get_video_details(stash_video_id: int) -> dict | None:
    """Get complete video details including performers, tags, studio, and URLs"""
    query = {
        "query": """
            query FindScene($id: ID!) {
                findScene(id: $id) {
                    id
                    title
                    details
                    date
                    rating100
                    organized
                    o_counter
                    urls
                    paths {
                        screenshot
                        preview
                        stream
                    }
                    files {
                        path
                        basename
                        size
                        duration
                        video_codec
                        audio_codec
                        width
                        height
                    }
                    performers {
                        name
                        gender
                        url
                        twitter
                        instagram
                        birthdate
                        ethnicity
                        country
                        hair_color
                        height_cm
                        measurements
                        fake_tits
                        tattoos
                        piercings
                        career_length
                    }
                    studio {
                        id
                        name
                        url
                    }
                    tags {
                        id
                        name
                        aliases
                        description
                    }
                    movies {
                        movie {
                            name
                            date
                        }
                    }
                    galleries {
                        title
                        url
                    }
                }
            }
        """,
        "variables": {"id": str(stash_video_id)}
    }

    try:
        logger.debug(f"Getting full details for scene ID: {stash_video_id}")
        response = await http_client.post(GRAPHQL_URL, json=query, headers=_graphql_headers())
        response.raise_for_status()
        data = response.json()

        if data.get("errors"):
            logger.error(f"GraphQL error getting scene details: {data['errors']}")
            return None

        scene = data.get("data", {}).get("findScene")
        if not scene:
            logger.warning(f"Scene {stash_video_id} not found")
            return None

        # Transform the data to a cleaner format
        video_details = {
            "id": scene["id"],
            "title": scene["title"],
            "details": scene.get("details", ""),
            "date": scene.get("date"),
            "rating": scene.get("rating100"),
            "urls": scene.get("urls", []),
            "duration": (scene.get("files") or [{}])[0].get("duration"),
            "resolution": (
                f"{scene['files'][0]['width']}x{scene['files'][0]['height']}"
                if scene.get("files") and scene["files"][0].get("width") and scene["files"][0].get("height")
                else None
            ),
            "files": scene.get("files", []),  # Include files array for fallback title
            "performers": [
                {
                    "name": p["name"],
                    "gender": p.get("gender"),
                    "url": p.get("url"),
                    "twitter": p.get("twitter"),
                    "instagram": p.get("instagram")
                } for p in scene.get("performers", [])
            ],
            "studio": {
                "name": scene.get("studio", {}).get("name"),
                "url": scene.get("studio", {}).get("url")
            } if scene.get("studio") else None,
            "tags": [
                {
                    "name": t["name"],
                    "description": t.get("description")
                } for t in scene.get("tags", [])
                if not (LIMIT_TO_TAG and str(t.get("id")) == str(LIMIT_TO_TAG))
            ],
            "movies": [m["movie"]["name"] for m in scene.get("movies", []) if m.get("movie")],
            "galleries": scene.get("galleries", [])
        }

        logger.info(f"Retrieved full details for scene {stash_video_id}")
        return video_details

    except Exception as e:
        logger.error(f"Error getting scene details for ID {stash_video_id}: {e}")
        return None
