"""Share-id / custom-slug generation and validation.

Slugs are unique across individual shares, tag shares, and static Markdown
pages (``data/pages/{slug}.md`` served at ``/{slug}``). They must also never
collide with a reserved app word/route, so a custom slug can never permanently
shadow a real route.
"""
from __future__ import annotations

import logging
import re
import secrets
from pathlib import Path

from fastapi import HTTPException
from sqids import Sqids

from sharestream.config import PAGES_DIR, SHARE_ID_LENGTH, SLUG_ALPHABET, SLUG_MIN_LENGTH
from sharestream.db.models import SharedTag, SharedVideo, VideoOverride

logger = logging.getLogger(__name__)

RESERVED_SLUGS = {
    "", "share", "tag", "edit_share", "delete_share", "share_tag",
    "shared_videos", "shared_tags", "delete_tag_share", "edit_tag_share",
    "lookup_tag", "get_video_title", "site_config", "login", "logout",
    "dmca", "report", "gallery", "static", "admin", "__admin", "video", "v", "api",
    "favicon.ico", "favicon.png", "robots.txt", "sitemap.xml", "thumbnail", "stream",
    "embed", "fonts.css", "filedrop", "og", "media",
}

# ------------------------------------------------------------------
# Video slugs (Sqids): reversible, non-sequential encoding of Stash scene ids
# for the canonical /v/{slug} route. encode/decode are pure (no DB), so routing
# a video needs no lookup, and an adjacent guess decodes to nothing.
#
# One Sqids instance is dedicated to videos. If tags ever need encoded slugs,
# instantiate a SEPARATE Sqids with a shifted alphabet so the two id spaces
# don't overlap (the spec currently routes tags by plain name, so just videos).
# ------------------------------------------------------------------
if SLUG_ALPHABET:
    _video_sqids = Sqids(alphabet=SLUG_ALPHABET, min_length=SLUG_MIN_LENGTH)
else:
    # No custom alphabet: Sqids still encodes/decodes fine, but output order is
    # the library default (not randomized for this deployment). Warn so the
    # operator knows to set a shuffled slug_alphabet for unguessable URLs.
    logger.warning("slug_alphabet not set; /v/ slugs use the default Sqids alphabet "
                   "and are not randomized for this deployment. Set a shuffled "
                   "62-char a-zA-Z0-9 slug_alphabet in config.yaml to randomize.")
    _video_sqids = Sqids(min_length=SLUG_MIN_LENGTH)


def encode_video_id(stash_video_id: int) -> str:
    """Encode a Stash scene id to its canonical video slug."""
    return _video_sqids.encode([int(stash_video_id)])


def decode_video_id(slug: str) -> int | None:
    """Decode a video slug back to its Stash scene id, or None if the slug is
    empty, undecodable, or NON-CANONICAL.

    Sqids can decode multiple distinct strings to the same number, which would
    let one video answer at many URLs. To keep exactly one canonical slug per
    scene we re-encode the decoded number and require it to match the input
    byte-for-byte; any non-canonical alias resolves to None (404)."""
    if not slug:
        return None
    numbers = _video_sqids.decode(slug)
    if not numbers:
        return None
    if _video_sqids.encode(numbers) != slug:
        return None
    return int(numbers[0])


def canonical_video_slug(db, stash_video_id: int) -> str:
    """The single canonical /v/ slug for a scene id: its VideoOverride.vanity_slug
    if one exists, else the Sqids encoding. Used by the canonical /v/ route and
    every legacy redirect shim so all old video URLs converge on one slug."""
    sid = int(stash_video_id)
    override = db.query(VideoOverride).filter(VideoOverride.stash_video_id == sid).first()
    if override is not None and override.vanity_slug:
        return override.vanity_slug
    return encode_video_id(sid)


def canonical_video_slugs(db, stash_video_ids) -> dict[int, str]:
    """Batch :func:`canonical_video_slug` for a list of scene ids — one query for
    all vanity slugs, Sqids-encoding the rest. Galleries use this to link every
    card to /v/{slug} without a per-card DB hit."""
    ids = {int(v) for v in stash_video_ids}
    if not ids:
        return {}
    vanity = {
        int(sid): slug
        for sid, slug in db.query(VideoOverride.stash_video_id, VideoOverride.vanity_slug)
        .filter(VideoOverride.stash_video_id.in_(ids))
        .filter(VideoOverride.vanity_slug.isnot(None))
        .all()
    }
    return {sid: vanity.get(sid) or encode_video_id(sid) for sid in ids}

# Slugs may only contain url-safe, unambiguous characters.
CUSTOM_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def generate_share_id() -> str:
    return secrets.token_urlsafe(SHARE_ID_LENGTH)


def normalize_custom_slug(slug: str) -> str:
    """Lower-case and strip a user-supplied slug to a safe canonical form."""
    return (slug or "").strip().lower()


def resolve_markdown_page_path(slug: str) -> Path | None:
    """Return ``data/pages/{slug}.md`` when it exists, else None.

    Slugs are normalized to lower case (matching share-id rules). Path
    traversal is rejected.
    """
    canonical = normalize_custom_slug(slug)
    if not canonical or not CUSTOM_SLUG_RE.match(canonical):
        return None
    pages_root = PAGES_DIR.resolve()
    path = (pages_root / f"{canonical}.md").resolve()
    if pages_root not in path.parents or path.suffix.lower() != ".md":
        return None
    return path if path.is_file() else None


def markdown_page_slug_taken(slug: str) -> bool:
    """True if ``slug`` maps to an on-disk Markdown page."""
    return resolve_markdown_page_path(slug) is not None


def validate_custom_share_id(slug: str, db) -> str:
    """Validate a custom slug and return its canonical form, or raise 400.

    Rejects reserved app words, bad characters, slugs already used by a static
    Markdown page, and any slug already in use by an existing individual or
    tag share (slugs are unique across both).
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
    if markdown_page_slug_taken(canonical):
        raise HTTPException(status_code=400, detail=f"'{canonical}' is already used by a site page.")
    if db.query(SharedVideo).filter(SharedVideo.share_id == canonical).first() or \
       db.query(SharedTag).filter(SharedTag.share_id == canonical).first():
        raise HTTPException(status_code=400, detail=f"Share ID '{canonical}' already exists.")
    return canonical
