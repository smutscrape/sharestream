"""Screenshot/thumbnail caching to the private shares dir.

Returns the PRIVATE on-disk Path (under ``data/shares``), never a public URL —
the files are served only via the access-checked thumbnail routes, so a
password-protected share's screenshot can't be fetched by guessing a /static URL.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from sharestream.backends.stash import screenshot_url
from sharestream.config import SHARES_DIR
from sharestream.core.http_client import http_client

logger = logging.getLogger(__name__)


async def fetch_and_cache_thumbnail(share_id: str, stash_video_id: int) -> Optional[Path]:
    """Cache the screenshot for an individual share. Returns its private path."""
    thumbnail_path = SHARES_DIR / f"{share_id}.jpg"
    try:
        if not thumbnail_path.exists():
            response = await http_client.get(screenshot_url(stash_video_id))
            if response.status_code == 200:
                with open(thumbnail_path, "wb") as f:
                    f.write(response.content)
                logger.info(f"Cached thumbnail for share_id={share_id} at {thumbnail_path}")
            else:
                logger.error(f"Failed to fetch thumbnail for share_id={share_id}: status={response.status_code}")
                return None
        return thumbnail_path
    except Exception as e:
        logger.error(f"Error fetching thumbnail for share_id={share_id}: {e}")
        return None


async def fetch_and_cache_tag_video_thumbnail(tag_share_id: str, video_id: int) -> Optional[Path]:
    """Cache the screenshot for a video within a tag share. Returns its private path."""
    thumbnail_path = SHARES_DIR / f"tag-{tag_share_id}-video-{video_id}.jpg"
    try:
        if not thumbnail_path.exists():
            response = await http_client.get(screenshot_url(video_id))
            if response.status_code == 200:
                with open(thumbnail_path, "wb") as f:
                    f.write(response.content)
                logger.info(f"Cached thumbnail for tag video {tag_share_id}/{video_id} at {thumbnail_path}")
            else:
                logger.error(f"Failed to fetch thumbnail for tag video {tag_share_id}/{video_id}: status={response.status_code}")
                return None
        return thumbnail_path
    except Exception as e:
        logger.error(f"Error fetching thumbnail for tag video {tag_share_id}/{video_id}: {e}")
        return None
