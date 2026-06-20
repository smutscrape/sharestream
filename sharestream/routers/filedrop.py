"""Public filedrop upload page: visitors upload videos that are ingested into Stash.

Stash has no byte-upload API, so an upload is streamed to a local temp file,
SFTP'd into a folder Stash watches, then scanned and (optionally) tagged. See
``services.filedrop`` for the delivery/scan logic and ``backends.stash`` for the
GraphQL mutations.

The whole feature is gated on ``FILEDROP_ENABLED``; every route 404s when it's
off, so the routes simply don't exist for an operator who hasn't opted in.
"""
from __future__ import annotations

import datetime
import logging
import os
import secrets
import tempfile
from datetime import timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from sharestream.config import (
    BASE_DOMAIN,
    DEFAULT_RESOLUTION,
    FILEDROP_ALLOW_USER_TAGS,
    FILEDROP_ALLOWED_EXTS,
    FILEDROP_AUTO_SHARE,
    FILEDROP_ENABLED,
    FILEDROP_MAX_UPLOAD_MB,
    FILEDROP_NEW_UPLOAD_TAGS,
    FILEDROP_PASSWORD,
)
from sharestream.backends.stash import add_tags_to_scene, update_scene_metadata
from sharestream.core.branding import site_context
from sharestream.core.security import pwd_context
from sharestream.core.templates import render
from sharestream.db.models import SharedVideo
from sharestream.db.session import get_db
from sharestream.services import access
from sharestream.services.filedrop import (
    find_public_view_share,
    get_public_tag_vocabulary,
    sanitize_filename,
    sftp_put,
    trigger_scan_and_tag,
)
from sharestream.services.media_proxy import generate_m3u8_file
from sharestream.services.slugs import generate_share_id

logger = logging.getLogger(__name__)

router = APIRouter()

_CHUNK = 1024 * 1024  # 1 MiB streaming chunk
# Lifetime of an auto-minted per-upload share link (a long-lived capability URL).
_AUTO_SHARE_DAYS = 3650


def _require_enabled():
    if not FILEDROP_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/filedrop", response_class=HTMLResponse, response_model=None)
async def filedrop_page(request: Request):
    """Render the upload page, or a password prompt when locked."""
    _require_enabled()
    locked = not access.filedrop_access_ok(request, FILEDROP_PASSWORD)
    context = site_context(request)
    context.update(
        locked=locked,
        max_upload_mb=FILEDROP_MAX_UPLOAD_MB,
        allowed_exts=sorted(FILEDROP_ALLOWED_EXTS),
        allow_user_tags=FILEDROP_ALLOW_USER_TAGS,
        error_message=None,
    )
    return HTMLResponse(render("filedrop.html", **context))


@router.post("/filedrop/verify", response_class=HTMLResponse, response_model=None)
async def filedrop_verify(request: Request, password: str = Form(...)):
    """Verify the filedrop password; on success set the unlock cookie + redirect."""
    _require_enabled()
    if access.filedrop_password_ok(password, FILEDROP_PASSWORD):
        resp = RedirectResponse("/filedrop", status_code=303)
        access.set_unlock_cookie(resp, access.FILEDROP_COOKIE_ID)
        return resp
    context = site_context(request)
    context.update(
        locked=True,
        max_upload_mb=FILEDROP_MAX_UPLOAD_MB,
        allowed_exts=sorted(FILEDROP_ALLOWED_EXTS),
        allow_user_tags=FILEDROP_ALLOW_USER_TAGS,
        error_message="Incorrect password. Please try again.",
    )
    return HTMLResponse(render("filedrop.html", **context), status_code=401)


@router.post("/filedrop/upload")
async def filedrop_upload(request: Request, file: UploadFile,
                          title: str = Form(""), description: str = Form("")):
    """Accept one uploaded video, deliver it to Stash, scan + tag it.

    Streams the body to a temp file (enforcing the size cap as it goes), SFTPs it
    to the Stash host, then triggers the scan/tag pipeline. Returns JSON the page
    uses to show per-file status.
    """
    _require_enabled()
    if not access.filedrop_access_ok(request, FILEDROP_PASSWORD):
        raise HTTPException(status_code=403, detail="Password required")

    orig_name = file.filename or "upload"
    ext = os.path.splitext(orig_name)[1].lower()
    if ext not in FILEDROP_ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail=f"File type '{ext or '(none)'}' not allowed")

    remote_name = sanitize_filename(orig_name)
    max_bytes = FILEDROP_MAX_UPLOAD_MB * 1024 * 1024

    tmp_fd, tmp_path = tempfile.mkstemp(prefix="filedrop_", suffix=ext)
    written = 0
    try:
        with os.fdopen(tmp_fd, "wb") as tmp:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=413,
                                        detail=f"File exceeds {FILEDROP_MAX_UPLOAD_MB} MB limit")
                tmp.write(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="Empty file")

        await sftp_put(tmp_path, remote_name)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Filedrop upload failed for '{orig_name}': {e}")
        raise HTTPException(status_code=502, detail="Upload delivery failed")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # Delivery succeeded; scan + tag is best-effort (returns status either way).
    try:
        outcome = await trigger_scan_and_tag(remote_name, orig_name,
                                             title=title, description=description)
    except Exception as e:
        logger.error(f"Filedrop scan/tag failed for '{remote_name}': {e}")
        outcome = {"status": "processing", "scene_id": None, "tagged": False}

    return JSONResponse({
        "filename": orig_name,
        "stored_as": remote_name,
        "status": outcome.get("status", "processing"),
        "scene_id": outcome.get("scene_id"),
        "tagged": outcome.get("tagged", False),
    })


@router.get("/filedrop/tags")
async def filedrop_tags(request: Request, db: Session = Depends(get_db)):
    """Public-tag vocabulary for the uploader's tag picker (autocomplete source).

    404s when user tagging is disabled so the capability isn't exposed at all."""
    _require_enabled()
    if not FILEDROP_ALLOW_USER_TAGS:
        raise HTTPException(status_code=404, detail="Not found")
    if not access.filedrop_access_ok(request, FILEDROP_PASSWORD):
        raise HTTPException(status_code=403, detail="Password required")
    return JSONResponse({"tags": await get_public_tag_vocabulary(db)})


@router.post("/filedrop/details")
async def filedrop_details(request: Request, scene_id: int = Form(...),
                           title: str = Form(""), description: str = Form(""),
                           tags: list[str] = Form(default=[]),
                           db: Session = Depends(get_db)):
    """Finalize a just-uploaded scene: set title/details, apply the uploader's
    chosen tags, and report whether the upload is now publicly viewable (or, if
    not and auto-share is on, a freshly-minted password-protected share link).

    The upload starts on drop (title defaulted to the filename); this lets the
    uploader refine title/description and pick tags without re-uploading. Blank
    title/description fields are left unchanged on Stash."""
    _require_enabled()
    if not access.filedrop_access_ok(request, FILEDROP_PASSWORD):
        raise HTTPException(status_code=403, detail="Password required")

    # Validate uploader-chosen tags against the public vocabulary (ignored
    # entirely when user tagging is disabled, so a forged form field is inert).
    user_tag_ids: list[str] = []
    if FILEDROP_ALLOW_USER_TAGS and tags:
        allowed = {t["id"] for t in await get_public_tag_vocabulary(db)}
        submitted = {str(t).strip() for t in tags if str(t).strip()}
        invalid = submitted - allowed
        if invalid:
            raise HTTPException(status_code=400, detail="Unknown or non-public tag selected")
        user_tag_ids = sorted(submitted)

    ok = await update_scene_metadata(scene_id, title=title.strip() or None,
                                     details=description.strip() or None)
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to update scene details")

    if user_tag_ids:
        await add_tags_to_scene(scene_id, user_tag_ids)

    # All tags now on the upload: the config-applied set plus the user's picks.
    assigned = set(FILEDROP_NEW_UPLOAD_TAGS) | set(user_tag_ids)

    result = {"scene_id": scene_id, "saved": True, "published": False,
              "view_url": None, "share_url": None, "password": None}

    # 1. Already publicly viewable via a no-password tag share?
    public_share = find_public_view_share(db, assigned)
    if public_share is not None:
        result["published"] = True
        result["view_url"] = f"{BASE_DOMAIN}/{public_share.share_id}/{scene_id}"
        return JSONResponse(result)

    # 2. Otherwise optionally mint a password-protected per-upload share link.
    if FILEDROP_AUTO_SHARE:
        password = secrets.token_urlsafe(9)
        share_id = generate_share_id()
        expires_at = datetime.datetime.now(timezone.utc) + datetime.timedelta(days=_AUTO_SHARE_DAYS)
        shared = SharedVideo(
            share_id=share_id,
            video_name=title.strip() or f"Scene {scene_id}",
            stash_video_id=scene_id,
            expires_at=expires_at,
            hits=0,
            resolution=DEFAULT_RESOLUTION,
            password_hash=pwd_context.hash(password),
            show_in_gallery=False,
        )
        db.add(shared)
        db.commit()
        if not await generate_m3u8_file(share_id, scene_id, DEFAULT_RESOLUTION):
            logger.error(f"Filedrop auto-share m3u8 generation failed for scene {scene_id}")
            # Keep the share row; playback will regenerate on demand if possible.
        result["share_url"] = f"{BASE_DOMAIN}/{share_id}?pwd={password}"
        result["password"] = password

    return JSONResponse(result)
