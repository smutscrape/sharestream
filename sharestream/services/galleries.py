"""Gallery/home page data building.

These builders assemble the template context (video/tag cards, pagination,
sorting, thumbnail warming) for the public home page, a tag-share gallery, and
the ``/gallery/tag/{name}`` view. Access control / expiry / password gating and
hit counting stay in the routers; these functions only wrangle display data.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import random
from datetime import timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from sharestream.backends.stash import (
    find_tag_by_name,
    get_all_videos_by_tag,
    get_tag_description,
    get_videos_by_tag,
)
from sharestream.config import (
    BASE_DOMAIN,
    GALLERY_HOME_MASONRY,
    GALLERY_HOME_PER_PAGE,
    GALLERY_PER_PAGE,
    VALID_SORTS,
    VISIBILITY_PUBLIC,
)
from sharestream.core.branding import site_context
from sharestream.db.models import SharedTag, SharedVideo
from sharestream.services.access import tag_share_respects_limit_tag
from sharestream.services.cache import prime_tag_membership
from sharestream.services.hits import get_total_plays_map
from sharestream.services.slugs import canonical_video_slugs
from sharestream.services.thumbnails import fetch_and_cache_thumbnail

logger = logging.getLogger(__name__)


def format_count(n: int) -> str:
    """Human-friendly count for headers: plain up to 1000, then 1-decimal 'k'.

    e.g. 305 -> '305', 1000 -> '1000', 5341 -> '5.3k', 12000 -> '12k'.
    """
    n = int(n or 0)
    if n > 1000:
        s = f"{n / 1000:.1f}".rstrip('0').rstrip('.')
        return f"{s}k"
    return str(n)


def normalize_sort(value) -> str | None:
    """Return a valid sort mode (lowercased), or None for unknown/empty input
    so callers can fall back to a per-share or the configured default."""
    if not value:
        return None
    v = str(value).strip().lower()
    return v if v in VALID_SORTS else None


def format_duration(seconds) -> str | None:
    """Compact runtime label for a card badge.

    < 90 seconds  -> whole seconds  ('66s')
    < 90 minutes  -> whole minutes  ('75m')
    otherwise     -> hours, 1 decimal ('1.5h')

    Returns None for unknown/zero durations so the template omits the badge.
    """
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    if s < 90:
        return f"{round(s)}s"
    minutes = s / 60
    if minutes < 90:
        return f"{round(minutes)}m"
    return f"{s / 3600:.1f}h"


def parse_aspect(resolution) -> float | None:
    """Aspect ratio (w/h) from a Stash "WxH" resolution string, or None.

    Used by the masonry gallery layout to size each card at its native aspect.
    Clamped to a sane range so a bogus dimension can't produce an extreme tile.
    """
    if not resolution or "x" not in str(resolution):
        return None
    try:
        w, h = str(resolution).lower().split("x", 1)
        ratio = float(w) / float(h)
    except (ValueError, ZeroDivisionError):
        return None
    if ratio <= 0:
        return None
    return max(0.4, min(ratio, 3.0))


def _title_sort_key(title):
    """Sort key for title/name ascending that pushes untitled videos to the END.

    Returns (is_empty, lowercased_title) so titled videos sort A->Z first and
    blank/whitespace-only titles fall last instead of clumping at the top.
    """
    t = (title or '').strip()
    return (t == '', t.lower())


async def _warm_thumbnails(coros, concurrency: int = 8) -> None:
    """Pre-warm on-disk thumbnail caches concurrently, with bounded fan-out.

    Galleries used to ``await`` each screenshot fetch one-by-one, so warming a
    screenful cost N sequential Stash round-trips before the page could render.
    Running them together (capped, so we don't open dozens of simultaneous Stash
    connections) collapses that to roughly ceil(N/concurrency) round-trips.
    Per-item errors are swallowed: a missing screenshot must not break the page —
    the gated thumbnail route falls back to the placeholder on its own.
    """
    sem = asyncio.Semaphore(concurrency)

    async def run(coro):
        async with sem:
            try:
                await coro
            except Exception:
                pass

    await asyncio.gather(*(run(c) for c in coros))


def effective_sort_date(date, created_at):
    """Release date if present, else Stash created_at, normalized to YYYY-MM-DD."""
    return (date or created_at or "")[:10]


def sort_video_dicts(items, sort):
    """Sort a list of Stash video dicts IN PLACE by the given mode.

    'date' (default) -> newest first by release date, falling back to created_at;
    'title' -> A→Z (normal alphabetical); 'hits'/'rating'/'duration' -> highest/longest first;
    'random' -> shuffled.
    """
    if sort == 'title':
        items.sort(key=lambda v: _title_sort_key(v.get('title')))
    elif sort == 'hits':
        items.sort(key=lambda v: v.get('hits') or 0, reverse=True)
    elif sort == 'rating':
        items.sort(key=lambda v: v.get('rating') or 0, reverse=True)
    elif sort == 'duration':
        items.sort(key=lambda v: v.get('duration') or 0, reverse=True)
    elif sort == 'random':
        random.shuffle(items)
    else:  # 'date' (default)
        items.sort(key=lambda v: effective_sort_date(v.get('date'), v.get('created_at')), reverse=True)


async def build_home_context(db: Session, request, sort: str = 'date', page: int = 1) -> dict:
    """Assemble the home-page template context (collections + combined gallery).

    Phase 2: the individual-video gallery is driven by the configured ``public``
    visibility tag (scenes carrying it), NOT by per-share ``show_in_gallery``
    flags. Curated tag-share Galleries (the "Collections" row) remain operator-
    curated showcases — orthogonal to scene visibility — so they still surface by
    ``show_in_gallery``.
    """
    if page < 1:
        page = 1
    # Get current time for expiration check
    current_time = datetime.datetime.now(timezone.utc)

    # Curated tag-share Galleries for the Collections row (operator showcases —
    # still flag-driven and independent of the public visibility tag).
    tag_shares = db.query(SharedTag).filter(
        SharedTag.expires_at > current_time,
        SharedTag.password_hash == None,
        SharedTag.show_in_gallery == True
    ).order_by(SharedTag.sort_order).all()

    # ---
    # Data wrangling
    # ---

    # 1. Collections row: fetch each curated Gallery's scenes concurrently for its
    # card (thumbnail + sample preview webps + count). These do NOT feed the main
    # gallery anymore — that's the public tag's job (step 2).
    tag_cards = []
    warm_coros = []  # thumbnails to pre-warm concurrently once the page is assembled
    tag_video_results = await asyncio.gather(
        *(get_videos_by_tag(tag.stash_tag_id) for tag in tag_shares)
    ) if tag_shares else []
    # Resolve slugs for collection-tag media URLs up front so cards use the
    # Hashid (non-sequential) — never the raw stash_video_id — in thumb/preview URLs.
    _tag_card_vids = set()
    for _t, (tag_videos, _tc) in zip(tag_shares, tag_video_results):
        for _v in (tag_videos or []):
            _tag_card_vids.add(int(_v["id"]))
    _tag_card_slugs = canonical_video_slugs(db, _tag_card_vids) if _tag_card_vids else {}

    for tag, (tag_videos, total_count) in zip(tag_shares, tag_video_results):
        if tag_videos:
            first_video = tag_videos[0]
            _first_id = int(first_video["id"])
            _first_sqid = _tag_card_slugs.get(_first_id, str(_first_id))
            warm_coros.append(fetch_and_cache_thumbnail(str(_first_id), _first_id))
            thumbnail_url = f"/media/{_first_sqid}/thumbnail.jpg"
            sample = random.sample(tag_videos, min(len(tag_videos), 12))
            preview_webps = [f"/media/{_tag_card_slugs.get(int(v['id']), int(v['id']))}/webp" for v in sample]
            tag_cards.append({
                "share_id": tag.share_id,
                "tag_name": tag.tag_name,
                "share_url": f"/{tag.share_id}",
                "thumbnail_url": thumbnail_url if thumbnail_url else "/static/default_thumbnail.jpg",
                "preview_webps": preview_webps,
                "video_count": total_count,
                "hits": tag.hits
            })

    # 2. Main gallery = scenes carrying the configured PUBLIC visibility tag.
    # If it's unset (operator hasn't configured visibility yet), show an empty
    # gallery and warn rather than crashing the live home page.
    public_videos = []
    if VISIBILITY_PUBLIC:
        public_videos = await get_all_videos_by_tag(VISIBILITY_PUBLIC, respect_limit_tag=False)
    else:
        logger.warning("visibility_tags.public is unset; home gallery is empty. "
                       "Set stash.visibility_tags.public in config to populate it.")

    # Aggregate play counts per scene in one batched lookup.
    public_ids = [int(v["id"]) for v in public_videos]
    total_plays = get_total_plays_map(db, public_ids)

    # Resolve every PUBLIC scene's slug up front so all generated URLs —
    # preview, thumbnail, share link Hashid, never the sequential id.
    slug_map = canonical_video_slugs(db, public_ids)

    # 3. Build a card per public-tagged scene. All generated media URLs use the
    # slug from slug_map; the stash id is tracked internally for sort/filter.
    all_video_cards = []
    for video in public_videos:
        vid = int(video["id"])
        vid_sqid = slug_map.get(vid, str(vid))
        all_video_cards.append({
            "video_name": video.get("title"),
            "preview_url": f"/media/{vid_sqid}/webp",
            "thumbnail_url": None,  # Will be filled below after pagination
            "lazy_thumbnail_url": f"/media/{vid_sqid}/thumbnail.jpg",
            "hits": total_plays.get(vid, 0),
            "duration": video.get("duration") or 0,
            "duration_label": format_duration(video.get("duration")),
            "stash_video_id": vid,
            "rating": video.get("rating") or 0,
            "sort_date": effective_sort_date(video.get("date"), video.get("created_at")),
            "aspect": parse_aspect(video.get("resolution")),
        })

    # 4. Sort the combined list
    if sort == 'title':
        all_video_cards.sort(key=lambda v: _title_sort_key(v.get('video_name')))
    elif sort == 'hits':
        all_video_cards.sort(key=lambda v: v.get('hits') or 0, reverse=True)
    elif sort == 'rating':
        all_video_cards.sort(key=lambda v: v.get('rating') or 0, reverse=True)
    elif sort == 'duration':
        all_video_cards.sort(key=lambda v: v.get('duration') or 0, reverse=True)
    elif sort == 'random':
        random.shuffle(all_video_cards)
    else:  # 'date' (default) — newest first by release date, else created_at
        all_video_cards.sort(key=lambda v: v.get('sort_date') or '', reverse=True)

    # Paginate the combined list the same way the tag galleries do: capture the
    # full total (for the header) BEFORE slicing to the current page's worth.
    per_page = GALLERY_HOME_PER_PAGE
    total_videos = len(all_video_cards)
    total_pages = (total_videos + per_page - 1) // per_page  # ceiling division
    has_more_pages = page < total_pages
    start = (page - 1) * per_page
    all_video_cards = all_video_cards[start:start + per_page]

    # Fill canonical /v/ link and thumbnail URL (now keyed to the slug).
    for card in all_video_cards:
        card['share_url'] = f"/v/{slug_map[int(card['stash_video_id'])]}"
        sqid = slug_map[int(card['stash_video_id'])]
        card['thumbnail_url'] = f"/media/{sqid}/thumbnail.jpg"
        warm_coros.append(fetch_and_cache_thumbnail(str(int(card['stash_video_id'])),
                                                   int(card['stash_video_id'])))

    await _warm_thumbnails(warm_coros)

    # Log final counts for debugging
    logger.info(f"Home page rendering: {len(tag_cards)} tag collections, "
                f"{len(all_video_cards)} of {total_videos} videos shown")

    context = site_context(request)
    context.update(
        tag_cards=tag_cards,
        all_video_cards=all_video_cards,  # Use the new combined and sorted list
        total_videos=total_videos,
        total_videos_label=format_count(total_videos),
        current_page=page,
        has_prev_page=page > 1,
        has_next_page=has_more_pages,
        prev_page_url=f"/?page={page-1}&sort={sort}" if page > 1 else None,
        next_page_url=f"/?page={page+1}&sort={sort}" if has_more_pages else None,
        sort=sort,
        home_gallery_masonry=GALLERY_HOME_MASONRY,
    )
    return context


async def build_tag_gallery_context(db: Session, tag_share: SharedTag, share_id: str,
                                    page: int = 1, sort: str = 'date', request=None) -> dict:
    """Assemble the paginated gallery context for a single tag share's page."""
    # Set pagination parameters
    per_page = GALLERY_PER_PAGE  # videos per page
    # Ensure page is at least 1
    if page < 1:
        page = 1

    # Get videos for this tag with pagination
    videos = []
    total_count = 0

    # Only a public, home-featured tag share stays limited to limit_to_tag.
    # A password-protected OR non-featured (capability-URL) share is a deliberate
    # share, so its own gallery shows the tag's full contents.
    respect_limit = tag_share_respects_limit_tag(tag_share.password_hash,
                                                 tag_share.show_in_gallery,
                                                 tag_share.apply_limit_tag)

    # Aggregate play counts per Stash scene (across every share context), so a
    # video's count matches the home page and its own video page. Populated once
    # the scene ids are known below.
    total_plays: dict[int, int] = {}

    if sort == 'random':
        # Let Stash handle random sort, paginating via Stash.
        _, total_count = await get_videos_by_tag(tag_share.stash_tag_id, per_page=1,
                                                 respect_limit_tag=respect_limit)  # get total count
        videos, _ = await get_videos_by_tag(tag_share.stash_tag_id, page=page, per_page=per_page,
                                            sort_by='random', respect_limit_tag=respect_limit)
        total_plays = get_total_plays_map(db, [int(v["id"]) for v in videos])
    else:
        # Fetch all, then sort in Python and paginate. This keeps Title a
        # normal A→Z sort and Date a release-date sort (with a created_at
        # fallback) — consistent with the home page.
        all_videos_raw = await get_all_videos_by_tag(tag_share.stash_tag_id,
                                                     respect_limit_tag=respect_limit)
        # We just fetched the tag's COMPLETE contents — seed the membership cache
        # so this page's own (access-gated) thumbnail sub-requests hit it warm
        # instead of each re-probing Stash.
        prime_tag_membership(tag_share.stash_tag_id,
                             (int(v["id"]) for v in all_videos_raw),
                             respect_limit_tag=respect_limit)
        total_plays = get_total_plays_map(db, [int(v["id"]) for v in all_videos_raw])
        if sort == 'hits':
            for video_raw in all_videos_raw:
                video_raw['hits'] = total_plays.get(int(video_raw["id"]), 0)
        sort_video_dicts(all_videos_raw, sort)
        total_count = len(all_videos_raw)
        start = (page - 1) * per_page
        videos = all_videos_raw[start:start + per_page]

    # Calculate pagination info
    total_pages = (total_count + per_page - 1) // per_page  # Ceiling division
    has_more_pages = page < total_pages

    # Transform videos for gallery display with proxied thumbnails. Warm the
    # first 20 screenshots concurrently (the rest lazy-load); either way the URL
    # is the access-gated route (so a protected tag's thumbnails stay behind its
    # password), never /static.
    slug_map = canonical_video_slugs(db, [int(v["id"]) for v in videos])
    video_cards = []
    warm_coros = []
    for i, video in enumerate(videos):
        vid = int(video["id"])
        thumb_route = f"/media/{slug_map[vid]}/thumbnail.jpg"
        eager = i < 20
        if eager:
            warm_coros.append(fetch_and_cache_thumbnail(str(vid), vid))

        video_cards.append({
            "video_name": video["title"],
            "share_url": f"/v/{slug_map[vid]}",
            "preview_url": f"/media/{slug_map.get(vid, vid)}/webp",
            "thumbnail_url": thumb_route if eager else "/static/default_thumbnail.jpg",
            "lazy_thumbnail_url": thumb_route if not eager else None,
            "hits": total_plays.get(vid, 0),
            "duration_label": format_duration(video.get("duration")),
            "aspect": parse_aspect(video.get("resolution")),
        })

    await _warm_thumbnails(warm_coros)

    # Prefer the tag's own Stash description for og:description (falls through to
    # site_description/site_motto in the template when the tag has none).
    tag_description = await get_tag_description(tag_share.stash_tag_id)

    context = site_context(request)
    context.update(
        video_cards=video_cards,
        current_page=page,
        has_prev_page=page > 1,
        has_next_page=has_more_pages,
        prev_page_url=f"/{share_id}?page={page-1}&sort={sort}" if page > 1 else None,
        next_page_url=f"/{share_id}?page={page+1}&sort={sort}" if has_more_pages else None,
        tag_name=tag_share.tag_name,
        total_videos=total_count,
        total_videos_label=format_count(total_count),
        sort=sort,
        gallery_mode=bool(tag_share.gallery_mode),
        # Social-embed: this share's negotiated collection thumbnail.
        collection_share_id=share_id,
        og_title=f"{tag_share.tag_name} ({format_count(total_count)} videos)",
        og_image=f"{BASE_DOMAIN}/tag/{share_id}/collection-thumb",
        page_url=f"{BASE_DOMAIN}/{share_id}",
        og_description=tag_description,
    )
    return context


async def build_tag_name_gallery_context(db: Session, tag_name: str, request=None) -> dict:
    """Assemble the ``/gallery/tag/{name}`` context: public shares whose videos
    carry the requested tag. Raises 404 if the tag is unknown to Stash."""
    # 1. Find tag_id from Stash
    tag_info = await find_tag_by_name(tag_name)
    if not tag_info:
        raise HTTPException(status_code=404, detail=f"Tag '{tag_name}' not found")

    tag_id = tag_info["id"]

    # 2. Find all videos with this tag from Stash
    target_videos_list = await get_all_videos_by_tag(tag_id)
    target_video_ids = {int(v['id']) for v in target_videos_list}
    # Runtime per scene (for the duration badge) from the same Stash payload.
    durations = {int(v['id']): v.get('duration') for v in target_videos_list}
    # One aggregate play-count lookup for every candidate scene, so counts match
    # the rest of the site regardless of which share surfaced the video here.
    total_plays = get_total_plays_map(db, target_video_ids)

    # If no videos have this tag, we can show an empty gallery
    if not target_video_ids:
        logger.info(f"No videos found for tag '{tag_name}' in Stash.")

    # 3. Get all active shares from DB. This by-tag gallery is public, so —
    # like the home gallery — it must EXCLUDE password-protected shares;
    # otherwise it would leak their thumbnails, names and links to anyone who
    # hits /gallery/tag/<name> without the password.
    current_time = datetime.datetime.now(timezone.utc)
    individual_shares = db.query(SharedVideo).filter(
        SharedVideo.expires_at > current_time,
        SharedVideo.password_hash == None,
    ).all()
    tag_shares = db.query(SharedTag).filter(
        SharedTag.expires_at > current_time,
        SharedTag.password_hash == None,
    ).all()

    video_cards = []
    processed_video_ids = set()
    warm_coros = []

    # 4. Process individual shares
    logger.info(f"Processing {len(individual_shares)} individual shares for tag '{tag_name}' gallery...")
    for video in individual_shares:
        if video.stash_video_id in target_video_ids and video.stash_video_id not in processed_video_ids:
            logger.debug(f"Found match in individual share: video_id={video.stash_video_id}")
            sid = video.stash_video_id
            warm_coros.append(fetch_and_cache_thumbnail(str(sid), sid))
            video_cards.append({
                "stash_video_id": sid,
                "preview_url": None,  # backfilled from slug_map below
                "video_name": video.video_name,
                "thumbnail_url": None,  # backfilled from slug_map below
                "hits": total_plays.get(sid, 0),
                "duration_label": format_duration(durations.get(sid)),
                "lazy_thumbnail_url": None,
            })
            processed_video_ids.add(sid)

    # 5. Process tag shares. Fetch each tag share's scenes concurrently, then
    # process the results in original order (preserving dedup behavior).
    logger.info(f"Processing {len(tag_shares)} tag shares for tag '{tag_name}' gallery...")
    tag_videos_lists = await asyncio.gather(
        *(get_all_videos_by_tag(t.stash_tag_id) for t in tag_shares)
    ) if tag_shares else []
    for tag_share, shared_videos in zip(tag_shares, tag_videos_lists):
        # We need all videos from this tag share to check against our target tag
        for video in shared_videos:
            video_id = int(video['id'])
            if video_id in target_video_ids and video_id not in processed_video_ids:
                logger.debug(f"Found match in tag share '{tag_share.tag_name}': video_id={video_id}")
                video_cards.append({
                    "stash_video_id": video_id,
                    "preview_url": None,
                    "video_name": video["title"],
                    "thumbnail_url": "/static/default_thumbnail.jpg",
                    "lazy_thumbnail_url": None,
                    "hits": total_plays.get(video_id, 0),
                    "duration_label": format_duration(video.get("duration")),
                })
                processed_video_ids.add(video_id)

    await _warm_thumbnails(warm_coros)

    # Resolve every card's Hashid slug in one batched query, then backfill ALL
    # generated URLs so nothing exposes the sequential stash id.
    slug_map = canonical_video_slugs(db, [c["stash_video_id"] for c in video_cards])
    for card in video_cards:
        sqid = slug_map[int(card["stash_video_id"])]
        card["preview_url"] = f"/media/{sqid}/webp"
        if card["thumbnail_url"] is None:
            card["thumbnail_url"] = f"/media/{sqid}/thumbnail.jpg"
        if card["lazy_thumbnail_url"] is None:
            card["lazy_thumbnail_url"] = f"/media/{sqid}/thumbnail.jpg"
        card["share_url"] = f"/v/{sqid}"

    # Sort video cards by name (A→Z), untitled videos last.
    video_cards.sort(key=lambda x: _title_sort_key(x.get('video_name')))

    total_videos = len(video_cards)
    logger.info(f"Rendering gallery for tag '{tag_name}' with {total_videos} videos.")

    context = site_context(request)
    context.update(
        tag_name=tag_name,
        video_cards=video_cards,
        total_videos=total_videos,
        total_videos_label=format_count(total_videos),
        current_page=None,  # Disabling pagination for this view
        has_prev_page=False,
        has_next_page=False,
        # Public aggregation page (not a single share): no per-share collection
        # thumbnail, so the template falls back to the site OG image.
        og_title=f"Tag: {tag_name}",
        page_url=f"{BASE_DOMAIN}/tag/{tag_name}",
    )
    return context
