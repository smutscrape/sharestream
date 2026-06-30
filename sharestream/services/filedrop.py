"""Filedrop ingestion: deliver an uploaded file into Stash's library and tag it.

Stash has no byte-upload API, so the flow is:
  1. the router streams the upload to a local temp file;
  2. :func:`sftp_put` copies it (over SSH, key auth + known_hosts) into the folder
     Stash watches (``FILEDROP_HOST_DIR`` on ``FILEDROP_SSH_HOST``);
  3. :func:`trigger_scan_and_tag` runs a Stash ``metadataScan`` of the SAME folder
     as Stash sees it (``FILEDROP_STASH_SCAN_PATH``), waits for the job, finds the
     new scene by its path, and applies ``FILEDROP_NEW_UPLOAD_TAGS`` if configured.

Path note: bytes are written to the HOST path; the scan + scene lookup use the
STASH-visible path (these differ when Stash runs in a container with a bind
mount). Both come from config.
"""
from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import shutil
import re
import secrets
import time
from threading import Lock

import asyncssh
from sqlalchemy.orm import Session

from sharestream.backends.stash import (
    add_tags_to_scene,
    find_scene_id_by_path,
    get_all_videos_by_tag,
    get_tags_for_scenes,
    metadata_generate,
    metadata_scan,
    update_scene_metadata,
    wait_for_job,
)
from sharestream.config import (
    FILEDROP_HOST_DIR,
    FILEDROP_NEW_UPLOAD_TAGS,
    FILEDROP_DISALLOWED_USER_TAGS,
    FILEDROP_SSH_HOST,
    FILEDROP_SSH_KEY,
    FILEDROP_SSH_PORT,
    FILEDROP_SSH_USER,
    FILEDROP_STASH_SCAN_PATH,
    LIMIT_TO_TAG,
)
from sharestream.db.models import SharedTag
from sharestream.services.access import tag_share_respects_limit_tag

logger = logging.getLogger(__name__)

_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Public-tag vocabulary cache: the set of tags that are already publicly
# browsable (carried by a video in any no-password share). TTL-cached so the
# autocomplete endpoint doesn't re-scan every share's Stash contents per request.
_VOCAB_TTL_SECONDS = 600.0
_vocab_cache: dict | None = None  # {"expires": float, "tags": list[{id,name}]}
_vocab_lock = Lock()


def default_title(original_name: str) -> str:
    """Title to use when the uploader didn't supply one: the ORIGINAL filename
    minus its extension (Stash leaves title blank on scan, and sharestream's
    gallery shows nothing without it)."""
    base = os.path.basename(original_name or "").strip()
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return stem.strip() or "Untitled"


def sanitize_filename(name: str) -> str:
    """Return a safe, collision-resistant basename for an uploaded file.

    Strips any directory components (no traversal), collapses unsafe characters,
    and prefixes a short random token so two uploads of the same name don't
    clash. Preserves the (sanitized) extension.
    """
    base = os.path.basename(name or "").strip() or "upload"
    # Split extension, sanitize both parts.
    stem, dot, ext = base.rpartition(".")
    if not dot:  # no extension
        stem, ext = base, ""
    stem = _SAFE_CHARS_RE.sub("_", stem).strip("._-") or "upload"
    ext = _SAFE_CHARS_RE.sub("", ext).lower()
    token = secrets.token_hex(4)
    safe = f"{token}_{stem}"
    if ext:
        safe = f"{safe}.{ext}"
    return safe


async def sftp_put(local_path: str, remote_name: str) -> str:
    """SFTP ``local_path`` to ``FILEDROP_HOST_DIR/remote_name`` on the Stash host.

    Returns the remote host path written. Uses key auth and verifies the host key
    against the default known_hosts. Raises on failure.
    """
    remote_path = posixpath.join(FILEDROP_HOST_DIR, remote_name)
    async with asyncssh.connect(
        FILEDROP_SSH_HOST,
        port=FILEDROP_SSH_PORT,
        username=FILEDROP_SSH_USER,
        client_keys=[FILEDROP_SSH_KEY],
    ) as conn:
        async with conn.start_sftp_client() as sftp:
            # Ensure the drop folder exists (idempotent).
            try:
                await sftp.makedirs(FILEDROP_HOST_DIR, exist_ok=True)
            except asyncssh.SFTPError:
                pass  # already exists / created concurrently
            await sftp.put(local_path, remote_path)
    logger.info(f"Filedrop SFTP delivered {remote_name} to {FILEDROP_SSH_HOST}:{remote_path}")
    return remote_path


async def local_put(local_path: str, remote_name: str) -> str:
    """Copy ``local_path`` to ``FILEDROP_HOST_DIR/remote_name`` on the local filesystem.

    Returns the destination path. Raises on failure.
    """
    dest_dir = FILEDROP_HOST_DIR
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, remote_name)
    shutil.copy2(local_path, dest_path)
    logger.info("Filedrop local copy delivered %s to %s", remote_name, dest_path)
    return dest_path


async def trigger_scan_and_tag(remote_name: str, original_name: str,
                               title: str | None = None,
                               description: str | None = None) -> dict:
    """Scan the drop folder, locate the new scene, set its title/details, tag it,
    and kick off cover/preview/sprite generation.

    The title defaults to the original filename minus extension (Stash leaves it
    blank on scan, and sharestream's gallery shows nothing without it); an
    uploader-supplied title/description override/augment it. Generation is fired
    so the scene gets a cover, preview clip, animated WebP, and sprites — without
    it the scene looks blank in the gallery.

    Returns ``{"status": "done"|"processing", "scene_id": int|None, "tagged": bool}``.
    Best-effort after delivery — the bytes are already on the Stash host, so a
    scan/lookup timeout returns "processing" rather than failing.
    """
    result = {"status": "processing", "scene_id": None, "tagged": False}
    job_id = await metadata_scan([FILEDROP_STASH_SCAN_PATH])
    if not job_id:
        return result
    await wait_for_job(job_id, timeout=180.0)

    scene_path = posixpath.join(FILEDROP_STASH_SCAN_PATH, remote_name)
    scene_id = await find_scene_id_by_path(scene_path)
    if scene_id is None:
        # Scanned but the scene isn't queryable yet (generation still running).
        return result

    result["scene_id"] = scene_id
    result["status"] = "done"

    effective_title = (title or "").strip() or default_title(original_name)
    await update_scene_metadata(scene_id, title=effective_title,
                                details=(description or "").strip() or None)

    if FILEDROP_NEW_UPLOAD_TAGS:
        result["tagged"] = await add_tags_to_scene(scene_id, FILEDROP_NEW_UPLOAD_TAGS)

    # Generate cover/previews/animated WebP/sprites so the scene renders properly
    # in the gallery. Fire-and-forget: we don't block the upload response on it.
    await metadata_generate([scene_id])
    return result


def find_public_view_share(db: Session, assigned_tag_ids) -> SharedTag | None:
    """Return a no-password tag share that makes an upload immediately viewable,
    or None.

    An upload assigned ``assigned_tag_ids`` is viewable through a public tag
    share when that share carries one of those tags AND the limit_to_tag
    requirement is satisfied: the share bypasses limit_to_tag (its
    ``tag_share_respects_limit_tag`` is False), limit_to_tag is unset, or
    limit_to_tag is itself among the assigned tags. The caller builds the view
    URL as ``/{share.share_id}/{scene_id}``.
    """
    from sharestream.db.models import VideoOverride
    assigned = {str(t) for t in assigned_tag_ids if t not in (None, "")}
    if not assigned:
        return None
    limit_ok_globally = (not LIMIT_TO_TAG) or (str(LIMIT_TO_TAG) in assigned)
    shares = db.query(SharedTag).filter(SharedTag.password_hash == None).all()  # noqa: E711
    for share in shares:
        if str(share.stash_tag_id) not in assigned:
            continue
        respects = tag_share_respects_limit_tag(
            share.password_hash, share.show_in_gallery, share.apply_limit_tag)
        if not respects or limit_ok_globally:
            return share
    return None


def clear_public_tag_vocabulary_cache() -> None:
    """Drop the cached public-tag vocabulary (e.g. after retagging in Stash)."""
    global _vocab_cache
    with _vocab_lock:
        _vocab_cache = None


async def get_public_tag_vocabulary(db: Session) -> list[dict]:
    """Return [{"id", "name", "count"}, ...] of every tag that is already publicly
    browsable — i.e. carried by a video in some no-password share (the tags that
    return content at ``/gallery/tag/{name}``). ``count`` is the number of public
    videos carrying the tag, used by the picker to rank suggestions. TTL-cached.

    Tags listed in ``FILEDROP_DISALLOWED_USER_TAGS`` are omitted: they may still
    be applied automatically via ``FILEDROP_NEW_UPLOAD_TAGS``, but uploaders may
    not select them manually.

    These are exactly the tags an uploader is allowed to self-assign: the picker
    offers them and the completion endpoint validates submissions against them.
    """
    from sharestream.db.models import VideoOverride

    global _vocab_cache
    now = time.time()
    with _vocab_lock:
        if _vocab_cache and _vocab_cache["expires"] > now:
            return _vocab_cache["tags"]

    # No-password shares define the public surface.
    tag_shares = db.query(SharedTag).filter(SharedTag.password_hash == None).all()  # noqa: E711
    individual = db.query(VideoOverride).filter(VideoOverride.password_hash == None).all()  # noqa: E711

    names: dict[str, str] = {}
    counts: dict[str, int] = {}

    def _tally(tid: str, name: str | None):
        names[tid] = name or tid
        counts[tid] = counts.get(tid, 0) + 1

    # 1. Tags carried by videos in each public tag share (fetched concurrently).
    if tag_shares:
        results = await asyncio.gather(*(
            get_all_videos_by_tag(
                t.stash_tag_id,
                respect_limit_tag=tag_share_respects_limit_tag(
                    t.password_hash, t.show_in_gallery, t.apply_limit_tag),
            )
            for t in tag_shares
        ))
        for videos in results:
            for v in videos:
                for tag in v.get("tags", []):
                    tid = str(tag.get("id"))
                    if tid and not (LIMIT_TO_TAG and tid == str(LIMIT_TO_TAG)):
                        _tally(tid, tag.get("name"))

    # 2. Tags on each individually-shared public video.
    if individual:
        scene_tags = await get_tags_for_scenes([v.stash_video_id for v in individual])
        for tags in scene_tags.values():
            for tag in tags:
                tid = str(tag.get("id"))
                if tid:
                    _tally(tid, tag.get("name"))

    vocab = sorted(
        ({"id": tid, "name": name, "count": counts.get(tid, 0)} for tid, name in names.items()),
        key=lambda t: t["name"].lower(),
    )

    # Remove operator-blocklisted tags from the uploader-facing vocabulary.
    # These may still be applied automatically via FILEDROP_NEW_UPLOAD_TAGS;
    # this only prevents manual selection.
    if FILEDROP_DISALLOWED_USER_TAGS:
        vocab = [t for t in vocab if str(t["id"]) not in FILEDROP_DISALLOWED_USER_TAGS]

    with _vocab_lock:
        _vocab_cache = {"expires": now + _VOCAB_TTL_SECONDS, "tags": vocab}  # noqa: F841
    return vocab
