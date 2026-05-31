"""Share-id / custom-slug generation and validation.

Slugs are unique across both individual and tag shares, and must never collide
with a reserved app word/route, so a custom slug can never permanently shadow a
real route.
"""
from __future__ import annotations

import re
import secrets

from fastapi import HTTPException

from sharestream.config import SHARE_ID_LENGTH
from sharestream.db.models import SharedTag, SharedVideo

RESERVED_SLUGS = {
    "", "share", "tag", "edit_share", "delete_share", "share_tag",
    "shared_videos", "shared_tags", "delete_tag_share", "edit_tag_share",
    "lookup_tag", "get_video_title", "site_config", "login", "logout",
    "dmca", "gallery", "static", "admin", "__admin", "video", "v", "api",
    "favicon.ico", "favicon.png", "robots.txt", "sitemap.xml", "thumbnail", "stream",
    "embed", "fonts.css",
}

# Slugs may only contain url-safe, unambiguous characters.
CUSTOM_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def generate_share_id() -> str:
    return secrets.token_urlsafe(SHARE_ID_LENGTH)


def normalize_custom_slug(slug: str) -> str:
    """Lower-case and strip a user-supplied slug to a safe canonical form."""
    return (slug or "").strip().lower()


def validate_custom_share_id(slug: str, db) -> str:
    """Validate a custom slug and return its canonical form, or raise 400.

    Rejects reserved app words, bad characters, and any slug already in use by
    an existing individual or tag share (slugs are unique across both).
    """
    canonical = normalize_custom_slug(slug)
    if not canonical:
        raise HTTPException(status_code=400, detail="Custom share ID cannot be empty.")
    if not CUSTOM_SLUG_RE.match(canonical):
        raise HTTPException(
            status_code=400,
            detail="Custom share ID may only contain letters, numbers, hyphens, and underscores.",
        )
    if canonical in RESERVED_SLUGS:
        raise HTTPException(status_code=400, detail=f"'{canonical}' is a reserved word and can't be used as a share ID.")
    if db.query(SharedVideo).filter(SharedVideo.share_id == canonical).first() or \
       db.query(SharedTag).filter(SharedTag.share_id == canonical).first():
        raise HTTPException(status_code=400, detail=f"Share ID '{canonical}' already exists.")
    return canonical
