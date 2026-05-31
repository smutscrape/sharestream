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
    get_scene_meta,
    get_videos_by_tag,
)
from sharestream.config import CONTENT_WARNING
from sharestream.core.branding import site_context
from sharestream.db.models import SharedTag, SharedVideo, TagVideoHit
from sharestream.services.thumbnails import (
    fetch_and_cache_tag_video_thumbnail,
    fetch_and_cache_thumbnail,
)

logger = logging.getLogger(__name__)


def effective_sort_date(date, created_at):
    """Release date if present, else Stash created_at, normalized to YYYY-MM-DD."""
    return (date or created_at or "")[:10]


def sort_video_dicts(items, sort):
    """Sort a list of Stash video dicts IN PLACE by the given mode.

    'date' (default) -> newest first by release date, falling back to created_at;
    'title' -> A→Z (normal alphabetical); 'hits'/'rating' -> highest first;
    'random' -> shuffled.
    """
    if sort == 'title':
        items.sort(key=lambda v: (v.get('title') or '').lower())
    elif sort == 'hits':
        items.sort(key=lambda v: v.get('hits') or 0, reverse=True)
    elif sort == 'rating':
        items.sort(key=lambda v: v.get('rating') or 0, reverse=True)
    elif sort == 'random':
        random.shuffle(items)
    else:  # 'date' (default)
        items.sort(key=lambda v: effective_sort_date(v.get('date'), v.get('created_at')), reverse=True)


async def build_home_context(db: Session, request, sort: str = 'date') -> dict:
    """Assemble the home-page template context (collections + combined gallery)."""
    # Get current time for expiration check
    current_time = datetime.datetime.now(timezone.utc)

    # Query for active, non-password-protected video shares that are set to show in gallery
    individual_videos = db.query(SharedVideo).filter(
        SharedVideo.expires_at > current_time,
        SharedVideo.password_hash == None,
        SharedVideo.show_in_gallery == True
    ).all()

    # Query for active, non-password-protected tag shares that are set to show in gallery
    tag_shares = db.query(SharedTag).filter(
        SharedTag.expires_at > current_time,
        SharedTag.password_hash == None,
        SharedTag.show_in_gallery == True
    ).order_by(SharedTag.sort_order).all()

    # ---
    # Data wrangling for combined gallery
    # ---

    # 1. Get all videos from gallery-enabled tag shares. Fetch every tag's
    # scenes concurrently (asyncio.gather) instead of awaiting them one-by-one,
    # so the page's upstream latency is ~one Stash round-trip rather than N. The
    # results are zipped back in tag order, so card order / dedup is unchanged.
    all_tag_videos = {}  # {video_id: video_info}
    tag_cards = []
    tag_video_results = await asyncio.gather(
        *(get_videos_by_tag(tag.stash_tag_id) for tag in tag_shares)
    ) if tag_shares else []
    for tag, (tag_videos, total_count) in zip(tag_shares, tag_video_results):
        # Create tag cards for collections display
        if tag_videos:
            first_video = tag_videos[0]
            # Warm the cache, then expose the access-gated route URL (never a
            # raw /static path). These tags are public, so the gate is a no-op.
            await fetch_and_cache_tag_video_thumbnail(tag.share_id, int(first_video["id"]))
            thumbnail_url = f"/tag/{tag.share_id}/thumbnail/{first_video['id']}"
            # A random sample of animated-WebP preview URLs from videos in
            # this tag, for the cycling animated collection card on the home
            # page. (Proxied on demand; nothing stored on disk.)
            sample = random.sample(tag_videos, min(len(tag_videos), 12))
            preview_webps = [f"/tag/{tag.share_id}/video/{v['id']}/webp" for v in sample]
            tag_cards.append({
                "share_id": tag.share_id,
                "tag_name": tag.tag_name,
                "share_url": f"/{tag.share_id}",
                "thumbnail_url": thumbnail_url if thumbnail_url else "/static/default_thumbnail.jpg",
                "preview_webps": preview_webps,
                "video_count": total_count,
                "hits": tag.hits
            })

        # Add videos to master list (deduplicating by video_id)
        for video in tag_videos:
            video_id = int(video["id"])
            if video_id not in all_tag_videos:
                all_tag_videos[video_id] = {
                    "video": video,
                    "tag_share_id": tag.share_id,
                    "tag_name": tag.tag_name,
                    "source": "tag"
                }

    # 2. Get rating + date metadata for all individual videos in one go
    individual_video_ids = [v.stash_video_id for v in individual_videos]
    meta = await get_scene_meta(individual_video_ids)

    # 3. Create a combined list of all video cards
    all_video_cards = []

    # Add individual video shares
    for video in individual_videos:
        m = meta.get(video.stash_video_id, {})
        all_video_cards.append({
            "share_id": video.share_id,
            "video_name": video.video_name,
            "share_url": f"/{video.share_id}",
            "preview_url": f"/share/{video.share_id}/webp",
            "thumbnail_url": None,  # Will be lazy loaded
            "lazy_thumbnail_url": f"/share/{video.share_id}/thumbnail.jpg",
            "hits": video.hits,
            "stash_video_id": video.stash_video_id,
            "rating": m.get("rating") or 0,
            "sort_date": effective_sort_date(m.get("date"), m.get("created_at"))
        })

    # Add videos from tag shares (avoiding duplicates)
    individual_video_ids_set = set(individual_video_ids)
    for video_id, video_info in all_tag_videos.items():
        if video_id not in individual_video_ids_set:
            video_data = video_info["video"]
            tag_share_id = video_info["tag_share_id"]

            hit_record = db.query(TagVideoHit).filter(
                TagVideoHit.tag_share_id == tag_share_id,
                TagVideoHit.video_id == video_id
            ).first()

            all_video_cards.append({
                "share_id": f"tag-{tag_share_id}-video-{video_id}",
                "video_name": video_data["title"],
                "share_url": f"/{tag_share_id}/{video_id}",
                "preview_url": f"/tag/{tag_share_id}/video/{video_id}/webp",
                "thumbnail_url": None,  # Will be lazy loaded
                "lazy_thumbnail_url": f"/tag/{tag_share_id}/thumbnail/{video_id}",
                "hits": hit_record.hits if hit_record else 0,
                "stash_video_id": video_id,
                "rating": video_data.get("rating", 0) or 0,
                "sort_date": effective_sort_date(video_data.get("date"), video_data.get("created_at"))
            })

    # 4. Sort the combined list
    if sort == 'title':
        all_video_cards.sort(key=lambda v: (v.get('video_name') or '').lower())
    elif sort == 'hits':
        all_video_cards.sort(key=lambda v: v.get('hits') or 0, reverse=True)
    elif sort == 'rating':
        all_video_cards.sort(key=lambda v: v.get('rating') or 0, reverse=True)
    elif sort == 'random':
        random.shuffle(all_video_cards)
    else:  # 'date' (default) — newest first by release date, else created_at
        all_video_cards.sort(key=lambda v: v.get('sort_date') or '', reverse=True)

    # Pre-warm the on-disk cache for the first batch of sorted videos so they
    # paint instantly, but always point the <img> at the access-gated route
    # URL (never a raw /static path).
    for i, card in enumerate(all_video_cards):
        if i >= 24:  # Number of thumbnails to preload
            break
        if card['share_id'].startswith('tag-') and '-video-' in card['share_id']:
            parts = card['share_id'].split('-video-')
            tag_share_id = parts[0][4:]
            video_id = int(parts[1])
            await fetch_and_cache_tag_video_thumbnail(tag_share_id, video_id)
            card['thumbnail_url'] = f"/tag/{tag_share_id}/thumbnail/{video_id}"
        else:
            await fetch_and_cache_thumbnail(card['share_id'], card['stash_video_id'])
            card['thumbnail_url'] = f"/share/{card['share_id']}/thumbnail.jpg"

    # Log final counts for debugging
    logger.info(f"Home page rendering: {len(tag_cards)} tag collections, {len(all_video_cards)} total videos")

    # Show the content warning only when configured AND the visitor hasn't
    # already acknowledged it (cookie set client-side on "Enter").
    show_content_warning = bool(CONTENT_WARNING) and not (
        request and request.cookies.get("content_warning_ack")
    )

    context = site_context()
    context.update(
        tag_cards=tag_cards,
        all_video_cards=all_video_cards,  # Use the new combined and sorted list
        content_warning=CONTENT_WARNING,
        show_content_warning=show_content_warning,
        sort=sort,
    )
    return context


async def build_tag_gallery_context(db: Session, tag_share: SharedTag, share_id: str,
                                    page: int = 1, sort: str = 'date') -> dict:
    """Assemble the paginated gallery context for a single tag share's page."""
    # Set pagination parameters
    per_page = 120  # Limit videos per page
    # Ensure page is at least 1
    if page < 1:
        page = 1

    # Get videos for this tag with pagination
    videos = []
    total_count = 0

    if sort == 'random':
        # Let Stash handle random sort, paginating via Stash.
        _, total_count = await get_videos_by_tag(tag_share.stash_tag_id, per_page=1)  # get total count
        videos, _ = await get_videos_by_tag(tag_share.stash_tag_id, page=page, per_page=per_page, sort_by='random')
    else:
        # Fetch all, then sort in Python and paginate. This keeps Title a
        # normal A→Z sort and Date a release-date sort (with a created_at
        # fallback) — consistent with the home page.
        all_videos_raw = await get_all_videos_by_tag(tag_share.stash_tag_id)
        if sort == 'hits':
            for video_raw in all_videos_raw:
                hit_record = db.query(TagVideoHit).filter(
                    TagVideoHit.tag_share_id == share_id,
                    TagVideoHit.video_id == int(video_raw["id"])
                ).first()
                video_raw['hits'] = hit_record.hits if hit_record else 0
        sort_video_dicts(all_videos_raw, sort)
        total_count = len(all_videos_raw)
        start = (page - 1) * per_page
        videos = all_videos_raw[start:start + per_page]

    # Calculate pagination info
    total_pages = (total_count + per_page - 1) // per_page  # Ceiling division
    has_more_pages = page < total_pages

    # Transform videos for gallery display with proxied thumbnails
    video_cards = []
    for i, video in enumerate(videos):
        # Get hit count for this video
        hit_record = db.query(TagVideoHit).filter(
            TagVideoHit.tag_share_id == share_id,
            TagVideoHit.video_id == int(video["id"])
        ).first()
        hits = hit_record.hits if hit_record else 0

        # Warm the cache for the first 20 so they paint instantly; the rest
        # lazy-load. Either way the URL is the access-gated route (so a
        # protected tag's thumbnails stay behind its password), never /static.
        thumb_route = f"/tag/{share_id}/thumbnail/{video['id']}"
        if i < 20:
            await fetch_and_cache_tag_video_thumbnail(share_id, int(video["id"]))
            eager = True
        else:
            eager = False

        video_cards.append({
            "share_id": f"tag-{share_id}-video-{video['id']}",
            "video_name": video["title"],
            "share_url": f"/{share_id}/{video['id']}",
            "preview_url": f"/tag/{share_id}/video/{video['id']}/webp",
            "thumbnail_url": thumb_route if eager else "/static/default_thumbnail.jpg",
            "lazy_thumbnail_url": thumb_route if not eager else None,
            "hits": hits
        })

    context = site_context()
    context.update(
        video_cards=video_cards,
        current_page=page,
        has_prev_page=page > 1,
        has_next_page=has_more_pages,
        prev_page_url=f"/tag/{share_id}?page={page-1}&sort={sort}" if page > 1 else None,
        next_page_url=f"/tag/{share_id}?page={page+1}&sort={sort}" if has_more_pages else None,
        tag_name=tag_share.tag_name,
        total_videos=len(video_cards),
        sort=sort,
    )
    return context


async def build_tag_name_gallery_context(db: Session, tag_name: str) -> dict:
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

    # 4. Process individual shares
    logger.info(f"Processing {len(individual_shares)} individual shares for tag '{tag_name}' gallery...")
    for video in individual_shares:
        if video.stash_video_id in target_video_ids and video.stash_video_id not in processed_video_ids:
            logger.debug(f"Found match in individual share: video_id={video.stash_video_id}")
            await fetch_and_cache_thumbnail(video.share_id, video.stash_video_id)
            video_cards.append({
                "share_url": f"/{video.share_id}",
                "preview_url": f"/share/{video.share_id}/webp",
                "video_name": video.video_name,
                "thumbnail_url": f"/share/{video.share_id}/thumbnail.jpg",
                "hits": video.hits,
                "lazy_thumbnail_url": None,
            })
            processed_video_ids.add(video.stash_video_id)

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
                # Use lazy loading for thumbnails here for performance
                thumbnail_url = "/static/default_thumbnail.jpg"
                lazy_thumbnail_url = f"/tag/{tag_share.share_id}/thumbnail/{video_id}"

                hit_record = db.query(TagVideoHit).filter(
                    TagVideoHit.tag_share_id == tag_share.share_id,
                    TagVideoHit.video_id == video_id
                ).first()
                hits = hit_record.hits if hit_record else 0

                video_cards.append({
                    "share_url": f"/{tag_share.share_id}/{video_id}",
                    "preview_url": f"/tag/{tag_share.share_id}/video/{video_id}/webp",
                    "video_name": video["title"],
                    "thumbnail_url": thumbnail_url,
                    "lazy_thumbnail_url": lazy_thumbnail_url,
                    "hits": hits,
                })
                processed_video_ids.add(video_id)

    # Sort video cards by name
    video_cards.sort(key=lambda x: x['video_name'])

    logger.info(f"Rendering gallery for tag '{tag_name}' with {len(video_cards)} videos.")

    context = site_context()
    context.update(
        tag_name=tag_name,
        video_cards=video_cards,
        current_page=None,  # Disabling pagination for this view
        has_prev_page=False,
        has_next_page=False,
    )
    return context
