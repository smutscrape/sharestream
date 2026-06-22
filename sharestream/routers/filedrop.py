"""Public filedrop upload page: visitors upload videos that are ingested into Stash.

Stash has no byte-upload API, so an upload is streamed to a local temp file,
SFTP'd into a folder Stash watches, then scanned and (optionally) tagged. See
``services.filedrop`` for the delivery/scan logic and ``backends.stash`` for the
GraphQL mutations.

The whole feature is gated on ``FILEDROP_ENABLED``; every route 404s when it's
off, so the routes simply don't exist for an operator who hasn't opted in.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import secrets
import tempfile
from datetime import timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel
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
    SMUTSCRAPE_API_TOKEN,
    SMUTSCRAPE_URL,
)
from sharestream.core.http_client import http_client
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
# SSE progress-poll timeout for scrape download jobs (30 minutes).
_SCRAPE_PROGRESS_TIMEOUT = 1800

# ---------------------------------------------------------------------------
# Pydantic models for scrape proxy routes
# ---------------------------------------------------------------------------
class ScrapeFetchRequest(BaseModel):
    url: str

class ScrapeStartRequest(BaseModel):
    url: str
    metadata_overrides: dict = {}
    user_tags: list[str] = []

class ScrapeCompleteRequest(BaseModel):
    job_id: str
    stash_scene_id: int

# ---------------------------------------------------------------------------
# In-memory store for metadata overrides (keyed by job_id).
# Populated by /scrape/start, consumed and cleared by /scrape/complete.
# ---------------------------------------------------------------------------
_scrape_overrides: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Smutscrape helpers
# ---------------------------------------------------------------------------
def _smutscrape_headers() -> dict:
    """Return auth headers for smutscrape if a token is configured."""
    h = {}
    if SMUTSCRAPE_API_TOKEN:
        h["Authorization"] = f"Bearer {SMUTSCRAPE_API_TOKEN}"
    return h

STATUS_MAP: dict[str, str] = {
    "queued": "pending",
    "scraping": "downloading",
    "postprocessing": "ingesting",
    "completed": "done",
    # passed through unchanged: downloading, ingesting, failed
}

def _format_speed(bytes_per_sec: float | None) -> str:
    if bytes_per_sec is None:
        return ""
    bps = float(bytes_per_sec)
    if bps >= 1024 * 1024:
        return f"{bps / (1024 * 1024):.1f} MiB/s"
    if bps >= 1024:
        return f"{bps / 1024:.1f} KiB/s"
    return f"{bps:.0f} B/s"

def _format_eta(seconds: int | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def _map_job_status(raw: str) -> str:
    """Map smutscrape job status to the frontend-facing status vocabulary."""
    return STATUS_MAP.get(raw, raw)


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


# ===========================================================================
# Scrape proxy routes — bridge to the smutscrape microservice.
# ===========================================================================


@router.post("/filedrop/scrape/fetch")
async def filedrop_scrape_fetch(request: Request, body: ScrapeFetchRequest):
    """Scrape metadata for a video URL via smutscrape (no download).

    Returns structured metadata the frontend uses to pre-populate the edit form."""
    _require_enabled()
    if not access.filedrop_access_ok(request, FILEDROP_PASSWORD):
        raise HTTPException(status_code=403, detail="Password required")
    if not SMUTSCRAPE_URL:
        raise HTTPException(status_code=503, detail="Smutscrape service not configured")

    try:
        resp = await http_client.post(
            f"{SMUTSCRAPE_URL}/scrape",
            json={"url": body.url, "download": False},
            headers=_smutscrape_headers(),
        )
    except Exception as e:
        logger.error(f"Smutscrape scrape/fetch unreachable: {e}")
        raise HTTPException(status_code=502, detail="Smutscrape service unreachable")

    if resp.status_code >= 400:
        detail = "Smutscrape request failed"
        try:
            detail = resp.json().get("detail", detail)
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=detail)

    data = resp.json()
    video = data.get("video") or {}
    return JSONResponse({
        "url": body.url,
        "metadata": {
            "title": video.get("title") or "",
            "description": video.get("description") or "",
            "tags": video.get("tags") or [],
            "performers": video.get("actors") or [],
            "studios": video.get("studios") or [],
            "image": video.get("image") or "",
        },
    })


@router.post("/filedrop/scrape/start")
async def filedrop_scrape_start(request: Request, body: ScrapeStartRequest,
                                 db: Session = Depends(get_db)):
    """Start a smutscrape download+ingest job for the given URL.

    The metadata_overrides are stored in-memory and applied to the Stash scene
    after the download completes (see /filedrop/scrape/complete)."""
    _require_enabled()
    if not access.filedrop_access_ok(request, FILEDROP_PASSWORD):
        raise HTTPException(status_code=403, detail="Password required")
    if not SMUTSCRAPE_URL:
        raise HTTPException(status_code=503, detail="Smutscrape service not configured")

    # Validate uploader-chosen tags against the public vocabulary (identical to
    # filedrop_details logic — inert when user tagging is disabled).
    user_tag_ids: list[str] = []
    if FILEDROP_ALLOW_USER_TAGS and body.user_tags:
        allowed = {t["id"] for t in await get_public_tag_vocabulary(db)}
        submitted = {str(t).strip() for t in body.user_tags if str(t).strip()}
        invalid = submitted - allowed
        if invalid:
            raise HTTPException(status_code=400, detail="Unknown or non-public tag selected")
        user_tag_ids = sorted(submitted)

    # Merge user-chosen tags with the operator's configured new_upload_tags.
    stash_tags = sorted(set(FILEDROP_NEW_UPLOAD_TAGS) | set(user_tag_ids))

    try:
        resp = await http_client.post(
            f"{SMUTSCRAPE_URL}/scrape",
            json={
                "url": body.url,
                "download": True,
                "stash_tags": stash_tags,
            },
            headers=_smutscrape_headers(),
        )
    except Exception as e:
        logger.error(f"Smutscrape scrape/start unreachable: {e}")
        raise HTTPException(status_code=502, detail="Smutscrape service unreachable")

    if resp.status_code >= 400:
        detail = "Smutscrape request failed"
        try:
            detail = resp.json().get("detail", detail)
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=detail)

    data = resp.json()
    job_ids = data.get("job_ids") or []
    if not job_ids:
        if data.get("skipped"):
            raise HTTPException(status_code=422, detail=data.get("skipped_reason", "Video skipped by smutscrape"))
        raise HTTPException(status_code=502, detail="Smutscrape returned no job ID")

    job_id = job_ids[0]
    _scrape_overrides[job_id] = body.metadata_overrides or {}

    return JSONResponse({"job_id": job_id, "status": "pending"})


@router.get("/filedrop/scrape/progress")
async def filedrop_scrape_progress(request: Request, job_id: str = Query(...)):
    """Server-Sent Events stream for a smutscrape download job's progress.

    Polls smutscrape GET /jobs/{job_id} every second, transforms the response
    into the frontend's expected format, and yields SSE events. The stream closes
    when the job reaches a terminal status (completed/failed) or times out."""
    _require_enabled()
    if not access.filedrop_access_ok(request, FILEDROP_PASSWORD):
        raise HTTPException(status_code=403, detail="Password required")
    if not SMUTSCRAPE_URL:
        raise HTTPException(status_code=503, detail="Smutscrape service not configured")

    async def event_stream():
        elapsed = 0.0
        terminal = {"completed", "failed"}
        try:
            while elapsed < _SCRAPE_PROGRESS_TIMEOUT:
                try:
                    resp = await http_client.get(
                        f"{SMUTSCRAPE_URL}/jobs/{job_id}",
                        headers=_smutscrape_headers(),
                    )
                except Exception as e:
                    logger.error(f"Smutscrape progress poll failed: {e}")
                    yield f"data: {json.dumps({'error': 'Smutscrape service unreachable'})}\n\n"
                    await asyncio.sleep(2)
                    elapsed += 2
                    continue

                if resp.status_code >= 400:
                    detail = "Job lookup failed"
                    try:
                        detail = resp.json().get("detail", detail)
                    except Exception:
                        pass
                    yield f"data: {json.dumps({'error': detail})}\n\n"
                    await asyncio.sleep(2)
                    elapsed += 2
                    continue

                job = resp.json()
                raw_status = job.get("status", "queued")
                mapped = _map_job_status(raw_status)

                progress_pct = job.get("progress_percent")
                if progress_pct is not None:
                    progress_pct = round(float(progress_pct), 1)

                sse_data = {
                    "job_id": job_id,
                    "status": mapped,
                    "progress": {
                        "percent": progress_pct,
                        "speed_str": _format_speed(job.get("speed")),
                        "eta_str": _format_eta(job.get("eta")),
                    },
                    "result": {
                        "stash_scene_id": job.get("scene_id"),
                    },
                    "error": job.get("error"),
                }
                yield f"data: {json.dumps(sse_data)}\n\n"

                if raw_status in terminal:
                    return

                await asyncio.sleep(1)
                elapsed += 1

            # Timeout
            yield f"data: {json.dumps({'job_id': job_id, 'status': 'failed', 'error': 'Job timed out after 30 minutes'})}\n\n"
        except asyncio.CancelledError:
            # Client disconnected; clean exit.
            pass

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/filedrop/scrape/complete")
async def filedrop_scrape_complete(request: Request, body: ScrapeCompleteRequest,
                                    db: Session = Depends(get_db)):
    """Finalize a scrape download: apply metadata overrides to the Stash scene
    and optionally mint an auto-share link (same logic as filedrop_details)."""
    _require_enabled()
    if not access.filedrop_access_ok(request, FILEDROP_PASSWORD):
        raise HTTPException(status_code=403, detail="Password required")

    scene_id = body.stash_scene_id
    overrides = _scrape_overrides.pop(body.job_id, {})

    # Apply metadata overrides to the Stash scene.
    title = (overrides.get("title") or "").strip()
    description = (overrides.get("description") or "").strip()
    if title or description:
        ok = await update_scene_metadata(scene_id, title=title or None,
                                         details=description or None)
        if not ok:
            logger.warning(f"Scrape complete: failed to update scene metadata for {scene_id}")

    # Validate + apply user-chosen tags (same pattern as filedrop_details).
    user_tag_ids: list[str] = []
    override_tags: list[str] = overrides.get("tags") or []
    if FILEDROP_ALLOW_USER_TAGS and override_tags:
        allowed = {t["id"] for t in await get_public_tag_vocabulary(db)}
        submitted = {str(t).strip() for t in override_tags if str(t).strip()}
        valid = sorted(submitted & allowed)
        if valid:
            await add_tags_to_scene(scene_id, valid)
            user_tag_ids = valid

    assigned = set(FILEDROP_NEW_UPLOAD_TAGS) | set(user_tag_ids)

    result = {"scene_id": scene_id, "published": False,
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
            video_name=title or f"Scene {scene_id}",
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
            logger.error(f"Scrape complete: auto-share m3u8 generation failed for scene {scene_id}")
        result["share_url"] = f"{BASE_DOMAIN}/{share_id}?pwd={password}"
        result["password"] = password

    return JSONResponse(result)
