"""Centralized access control: expiry checks, password-unlock cookies, the
human-facing password prompt, and the media-subrequest access gate.

This is the single owner of "is the caller allowed to see this?" logic so the
same expiry/password (and, for tag videos, membership) checks aren't reinvented
in every route. ``share_id`` here always means the id the unlock cookie is keyed
to — for tag videos that is the TAG share id, not the composite media id.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
from datetime import timezone
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from sharestream.config import SECRET_KEY
from sharestream.core.security import pwd_context
from sharestream.core.templates import render
from sharestream.core.branding import site_context
from sharestream.db.models import SharedTag, SharedVideo
from sharestream.db.session import SessionLocal
from sharestream.services.cache import is_video_in_tag


# ------------------------------------------------------------------
# Expiry
# ------------------------------------------------------------------
def is_expired(expires_at) -> bool:
    """True if a naive/aware ``expires_at`` is in the past (treated as UTC)."""
    return expires_at.replace(tzinfo=timezone.utc) < datetime.datetime.now(timezone.utc)


def ensure_not_expired(expires_at, detail: str) -> None:
    """Raise 403 with ``detail`` if the share has expired."""
    if is_expired(expires_at):
        raise HTTPException(status_code=403, detail=detail)


# ------------------------------------------------------------------
# Password-protected share verification (signed cookie)
# ------------------------------------------------------------------
# After a correct password, we set an HMAC cookie keyed to the share_id instead
# of trusting a forgeable URL flag. The cookie proves password knowledge for
# that one share; it can't be guessed or reused across shares.
def _pw_cookie_name(share_id: str) -> str:
    return "pwok_" + hashlib.sha256(share_id.encode()).hexdigest()[:16]


def _pw_token(share_id: str) -> str:
    return hmac.new(SECRET_KEY.encode(), share_id.encode(), hashlib.sha256).hexdigest()


def has_valid_pw_cookie(request: Request, share_id: str) -> bool:
    if request is None:
        return False
    return hmac.compare_digest(request.cookies.get(_pw_cookie_name(share_id), ""), _pw_token(share_id))


def set_unlock_cookie(response, share_id: str) -> None:
    """Set the signed, share-scoped unlock cookie. Single source of truth shared
    by /share/{id}/verify and the ?pwd= page flow, so both issue an identical
    cookie (httponly, Secure, SameSite=Lax, 7-day, path=/)."""
    response.set_cookie(
        _pw_cookie_name(share_id),
        _pw_token(share_id),
        max_age=7 * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=True,
        path="/",
    )


def verify_password(password: str, password_hash: Optional[str]) -> bool:
    """True if ``password`` matches the bcrypt ``password_hash``."""
    if not password_hash:
        return False
    return pwd_context.verify(password, password_hash)


def safe_next_path(next_url: Optional[str]) -> Optional[str]:
    """Return ``next_url`` only if it's a safe same-origin path, else None.

    Guards the post-unlock redirect against open-redirect / protocol-relative
    abuse: must be a root-relative path ("/...") and never "//host" or a
    backslash-obfuscated variant.
    """
    if not next_url:
        return None
    if not next_url.startswith("/"):
        return None
    if next_url.startswith("//"):
        return None
    if "\\" in next_url:
        return None
    return next_url


def _current_path(request: Request) -> str:
    """The current request's path (+query), with any ?pwd= stripped — the page
    the viewer asked for, used as the post-unlock redirect target."""
    if request is None:
        return ""
    clean = request.url.remove_query_params("pwd")
    return clean.path + (f"?{clean.query}" if clean.query else "")


def media_access_ok(request: Request, share_id: str, password_hash: Optional[str]) -> bool:
    """Access check for MEDIA sub-requests (HLS playlists/segments, mp4, webp,
    thumbnails, previews). Returns True if the share is public OR the browser
    presents the share's unlock cookie.

    Cookie-only by design: the unlock cookie is set when a human passes the
    password prompt on the share's page, and the browser then sends it with every
    same-origin media request. We deliberately do NOT honour a ?pwd= query here
    (a) so password-protected media can't be hot-linked/embedded with the password
    baked into a URL, and (b) to avoid running bcrypt on every segment request
    (which would put expensive hashing on the event loop and invite a cheap DoS).
    `share_id` must be the id the cookie is keyed to — for tag videos that is the
    TAG share id, not the composite tag-<id>-video-<id> media id."""
    if not password_hash:
        return True
    return has_valid_pw_cookie(request, share_id)


def password_prompt_if_locked(request: Request, share_id: str,
                              password_hash: Optional[str], display_name: str):
    """Gate for human-facing PAGES. If the share is password-protected and the
    request hasn't unlocked it (valid cookie, or a correct ?pwd= on first visit),
    return the password-prompt HTMLResponse for the caller to return immediately;
    otherwise return None and the caller proceeds. `share_id` must be the id the
    unlock cookie is/should be keyed to (the TAG share id for tag pages)."""
    if not password_hash:
        return None
    if has_valid_pw_cookie(request, share_id):
        return None
    url_password = (request.query_params.get('pwd', '') if request else '') or ''
    if url_password and pwd_context.verify(url_password, password_hash):
        # Correct password in the URL: convert it into the signed unlock cookie
        # (so cookie-only media subrequests succeed) and redirect to the same page
        # with ?pwd stripped — preserving any other params like page/sort — so the
        # secret doesn't linger in the address bar, history, or Referer headers.
        clean = request.url.remove_query_params("pwd")
        target = clean.path + (f"?{clean.query}" if clean.query else "")
        resp = RedirectResponse(target, status_code=303)
        set_unlock_cookie(resp, share_id)
        return resp
    html = render(
        "password-prompt.html",
        **site_context(),
        video_name=display_name,
        share_id=share_id,
        url_password=url_password,
        error_message=None,
        # Where to send the viewer after they unlock: the exact page they asked
        # for (e.g. /some-tag/12345), not just the tag gallery.
        next_url=_current_path(request),
    )
    return HTMLResponse(html)


# ------------------------------------------------------------------
# Filedrop gate (plaintext config password + signed unlock cookie)
# ------------------------------------------------------------------
# The filedrop password lives in config as plaintext (operator's choice), so we
# compare it directly rather than via bcrypt. Once a visitor passes it we issue
# the SAME signed unlock cookie used for share passwords, keyed to "filedrop", so
# the upload endpoint can authorize cookie-only (no password echoed per request).
FILEDROP_COOKIE_ID = "filedrop"


def filedrop_access_ok(request: Request, configured_password: str) -> bool:
    """True if filedrop is open (no password) OR the request carries the unlock
    cookie. Used to gate both the page and the upload endpoint."""
    if not configured_password:
        return True
    return has_valid_pw_cookie(request, FILEDROP_COOKIE_ID)


def filedrop_password_ok(submitted: str, configured_password: str) -> bool:
    """Constant-time compare of a submitted filedrop password to the config one."""
    if not configured_password:
        return True
    return hmac.compare_digest(str(submitted or ""), configured_password)


# ------------------------------------------------------------------
# limit_to_tag scope for a tag share's OWN surfaces
# ------------------------------------------------------------------
def tag_share_respects_limit_tag(password_hash: Optional[str], show_in_gallery: bool,
                                 apply_limit_tag: bool = True) -> bool:
    """Whether the global ``limit_to_tag`` filter applies to a tag share's OWN
    pages/media (its ``/tag/{share_id}`` gallery, ``/tag/{share_id}/video/{id}``
    page, and that share's media sub-requests).

    A PUBLIC, home-featured tag share is ALWAYS limited (un-curated videos must
    never surface on the public site). A share that is password-protected OR not
    featured is a deliberate, capability-URL share; for it the operator chooses
    per share via ``apply_limit_tag`` (default True) whether to keep the filter or
    expose the tag's full contents.

    This governs ONLY a share's own surfaces. The public aggregation pages (the
    home gallery and ``/gallery/tag/{name}``) always apply the filter regardless,
    so non-curated videos never surface while browsing the site.
    """
    if password_hash is None and show_in_gallery:
        return True  # featured public share: always limited
    return bool(apply_limit_tag)  # non-public: operator's per-share choice


# ------------------------------------------------------------------
# Media authorization for a resolved share/video
# ------------------------------------------------------------------
async def authorize_media(request: Request, resolved) -> None:
    """Enforce expiry + password (+ tag membership for tag videos) on a media
    sub-request for a :class:`~sharestream.services.resolver.ResolvedMedia`.

    Raises the same HTTPExceptions the original per-route checks did.
    """
    expired_detail = "Tag share has expired" if resolved.is_tag_video else "Share link has expired"
    ensure_not_expired(resolved.expires_at, expired_detail)

    if not media_access_ok(request, resolved.cookie_share_id, resolved.password_hash):
        raise HTTPException(status_code=403, detail="Password required")

    if resolved.is_tag_video:
        # Only a public, home-featured tag share is limited to limit_to_tag;
        # password-protected OR non-featured (capability-URL) shares reach the
        # tag's full contents, so membership is checked against the full set.
        respect_limit = tag_share_respects_limit_tag(resolved.password_hash,
                                                     resolved.show_in_gallery,
                                                     resolved.apply_limit_tag)
        if not await is_video_in_tag(resolved.stash_tag_id, resolved.stash_video_id,
                                     respect_limit_tag=respect_limit):
            raise HTTPException(status_code=404, detail="Video not found in this tag")


async def authorize_tag_video(request: Request, share_id: str, video_id: int) -> SharedTag:
    """Gate a media sub-request for a specific video within a tag share.

    Looks up the tag share, enforces expiry + password + tag membership, and
    returns the SharedTag. Raises the same HTTPExceptions as the original routes.

    Owns a short-lived session for the lookup and CLOSES it before the
    (network-bound, potentially slow) membership check, so a DB connection is
    never held across that ``await``. Otherwise a burst of media sub-requests —
    e.g. a gallery rendering dozens of tag-video thumbnails at once — would each
    pin a connection while awaiting the single coalesced membership fetch and
    exhaust the pool (the membership fetch can list an entire large tag).
    """
    with SessionLocal() as db:
        tag_share = db.query(SharedTag).filter(SharedTag.share_id == share_id).first()
        if not tag_share:
            raise HTTPException(status_code=404, detail="Tag share not found")
        # Capture the plain values the post-close checks need; the detached
        # instance keeps its already-loaded columns (resolution, etc.) for the
        # caller, but we read these here so nothing lazy-loads after close.
        password_hash = tag_share.password_hash
        stash_tag_id = tag_share.stash_tag_id
        expires_at = tag_share.expires_at
        show_in_gallery = tag_share.show_in_gallery
        apply_limit_tag = tag_share.apply_limit_tag
        db.expunge(tag_share)
    # ---- session released; no DB connection held past this point ----
    ensure_not_expired(expires_at, "Tag share has expired")
    if not media_access_ok(request, share_id, password_hash):
        raise HTTPException(status_code=403, detail="Password required")
    # A featured public share is always limited; a non-public share follows its
    # operator's per-share apply_limit_tag choice.
    respect_limit = tag_share_respects_limit_tag(password_hash, show_in_gallery, apply_limit_tag)
    if not await is_video_in_tag(stash_tag_id, video_id, respect_limit_tag=respect_limit):
        raise HTTPException(status_code=404, detail="Video not found in this tag")
    return tag_share


def authorize_tag_share(request: Request, db: Session, share_id: str,
                        expired_detail: str = "Tag share has expired") -> SharedTag:
    """Gate a media sub-request for a tag SHARE as a whole (its collection-thumb),
    enforcing expiry + password keyed to the tag share id.

    Parallels :func:`authorize_share_media` but for the collection (no single
    video / membership check). A password-protected tag share therefore won't
    expose a collection preview to an anonymous crawler — the same privacy stance
    as the embed player.
    """
    tag_share = db.query(SharedTag).filter(SharedTag.share_id == share_id).first()
    if not tag_share:
        raise HTTPException(status_code=404, detail="Tag share not found")
    ensure_not_expired(tag_share.expires_at, expired_detail)
    if not media_access_ok(request, share_id, tag_share.password_hash):
        raise HTTPException(status_code=403, detail="Password required")
    return tag_share


def authorize_share_media(request: Request, db: Session, share_id: str,
                          expired_detail: str) -> SharedVideo:
    """Gate a media sub-request for an individual share (expiry + password).

    Returns the SharedVideo. ``expired_detail`` lets callers preserve the exact
    legacy wording ("Share has expired" for thumbnail/preview routes).
    """
    video = db.query(SharedVideo).filter(SharedVideo.share_id == share_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Share not found")
    ensure_not_expired(video.expires_at, expired_detail)
    if not media_access_ok(request, share_id, video.password_hash):
        raise HTTPException(status_code=403, detail="Password required")
    return video
