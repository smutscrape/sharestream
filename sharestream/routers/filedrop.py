"""Public filedrop upload page: visitors upload videos that are ingested into Stash.

Stash has no byte-upload API, so an upload is streamed to a local temp file,
SFTP'd into a folder Stash watches, then scanned and (optionally) tagged. See
``services.filedrop`` for the delivery/scan logic and ``backends.stash`` for the
GraphQL mutations.

The whole feature is gated on ``FILEDROP_ENABLED``; every route 404s when it's
off, so the routes simply don't exist for an operator who hasn't opted in.
"""
from __future__ import annotations

import logging
import os
import tempfile

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from sharestream.config import (
    FILEDROP_ALLOWED_EXTS,
    FILEDROP_ENABLED,
    FILEDROP_MAX_UPLOAD_MB,
    FILEDROP_PASSWORD,
)
from sharestream.core.branding import site_context
from sharestream.core.templates import render
from sharestream.services import access
from sharestream.services.filedrop import sanitize_filename, sftp_put, trigger_scan_and_tag

logger = logging.getLogger(__name__)

router = APIRouter()

_CHUNK = 1024 * 1024  # 1 MiB streaming chunk


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
        error_message="Incorrect password. Please try again.",
    )
    return HTMLResponse(render("filedrop.html", **context), status_code=401)


@router.post("/filedrop/upload")
async def filedrop_upload(request: Request, file: UploadFile):
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
        outcome = await trigger_scan_and_tag(remote_name)
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
