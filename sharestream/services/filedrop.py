"""Filedrop ingestion: deliver an uploaded file into Stash's library and tag it.

Stash has no byte-upload API, so the flow is:
  1. the router streams the upload to a local temp file;
  2. :func:`sftp_put` copies it (over SSH, key auth + known_hosts) into the folder
     Stash watches (``FILEDROP_HOST_DIR`` on ``FILEDROP_SSH_HOST``);
  3. :func:`trigger_scan_and_tag` runs a Stash ``metadataScan`` of the SAME folder
     as Stash sees it (``FILEDROP_STASH_SCAN_PATH``), waits for the job, finds the
     new scene by its path, and applies ``FILEDROP_TAG_ID`` if configured.

Path note: bytes are written to the HOST path; the scan + scene lookup use the
STASH-visible path (these differ when Stash runs in a container with a bind
mount). Both come from config.
"""
from __future__ import annotations

import logging
import os
import posixpath
import re
import secrets

import asyncssh

from sharestream.backends.stash import (
    add_tag_to_scene,
    find_scene_id_by_path,
    metadata_scan,
    wait_for_job,
)
from sharestream.config import (
    FILEDROP_HOST_DIR,
    FILEDROP_SSH_HOST,
    FILEDROP_SSH_KEY,
    FILEDROP_SSH_PORT,
    FILEDROP_SSH_USER,
    FILEDROP_STASH_SCAN_PATH,
    FILEDROP_TAG_ID,
)

logger = logging.getLogger(__name__)

_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


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


async def trigger_scan_and_tag(remote_name: str) -> dict:
    """Scan the drop folder in Stash, locate the new scene, and tag it.

    Returns a status dict: ``{"status": "done"|"processing", "scene_id": int|None,
    "tagged": bool}``. Best-effort after delivery — the bytes are already on the
    Stash host, so a scan/lookup timeout returns "processing" rather than failing.
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
    if FILEDROP_TAG_ID:
        result["tagged"] = await add_tag_to_scene(scene_id, FILEDROP_TAG_ID)
    return result
