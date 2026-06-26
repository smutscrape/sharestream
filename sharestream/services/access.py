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

from sharestream.config import (
    LIMIT_TO_TAG,
    SECRET_KEY,
    VISIBILITY_HIDDEN,
    VISIBILITY_LISTED,
    VISIBILITY_PUBLIC,
)
from sharestream.core.security import pwd_context
from sharestream.core.templates import render
from sharestream.core.branding import site_context
from sharestream.db.models import SharedTag, SharedVideo, VideoOverride
from sharestream.db.session import SessionLocal
from sharestream.services.cache import get_scene_tag_ids, is_video_in_tag

# Access outcomes returned by resolve_scene_access / authorize_scene_media-style
# gates. Stable trio so the /v/ route and media routes don't change.
ACCESS_ALLOW = "ALLOW"
ACCESS_PASSWORD_REQUIRED = "PASSWORD_REQUIRED"
ACCESS_NOT_FOUND = "NOT_FOUND"

# Visibility levels (for Cache-Control selection and listing logic).
VIS_PUBLIC = "public"
VIS_LISTED = "listed"
VIS_UNLISTED = "unlisted"
VIS_HIDDEN = "hidden"


def scene_visibility(tag_ids: set[str]) -> str:
    """Classify a scene's visibility from the set of Stash tag ids it carries.
    hidden overrides all; then public, then listed; otherwise unlisted. Levels
    whose tag is unconfigured (None) simply never match."""
    if VISIBILITY_HIDDEN and VISIBILITY_HIDDEN in tag_ids:
        return VIS_HIDDEN
    if VISIBILITY_PUBLIC and VISIBILITY_PUBLIC in tag_ids:
        return VIS_PUBLIC
    if VISIBILITY_LISTED and VISIBILITY_LISTED in tag_ids:
        return VIS_LISTED
    return VIS_UNLISTED


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
                              password_hash: Optional[str], display_name: str,
                              verify_action: Optional[str] = None):
    """Gate for human-facing PAGES. If the share is password-protected and the
    request hasn't unlocked it (valid cookie, or a correct ?pwd= on first visit),
    return the password-prompt HTMLResponse for the caller to return immediately;
    otherwise return None and the caller proceeds. `share_id` must be the id the
    unlock cookie is/should be keyed to (the TAG share id for tag pages).

    ``verify_action`` overrides the form's POST target; defaults (in the template)
    to ``/share/{share_id}/verify``. The /v/ route passes ``/v/{slug}/verify``."""
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
        verify_action=verify_action,
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
# limit_to_tag scope for a curated Gallery's OWN surfaces
# ------------------------------------------------------------------
# NOTE (Phase 2): limit_to_tag is RETIRED as a scene-access gate — `/v/{slug}`
# and `/media/{id}` access is governed by resolve_scene_access / the visibility
# tags, NOT this function. This now only scopes a curated Gallery (SharedTag)
# render at `/{slug}` (which videos that page lists). It does not decide whether a
# video is reachable.
def tag_share_respects_limit_tag(password_hash: Optional[str], show_in_gallery: bool,
                                 apply_limit_tag: bool = True) -> bool:
    """Whether the global ``limit_to_tag`` filter applies to a curated Gallery's
    OWN listing (which videos appear when rendering the Gallery at ``/{slug}``).

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


# ------------------------------------------------------------------
# Scene-keyed access (canonical /v/ + /media/{id})
# ------------------------------------------------------------------
# Per-video unlock cookies are keyed to the SCENE id (str(stash_video_id)), not a
# share id, so a password set via VideoOverride follows the video across every
# entry path. set_unlock_cookie/has_valid_pw_cookie are reused with this key.
#
# Visibility is config-driven (visibility_tags: public/listed/hidden). Access and
# *listing* are separate concerns: this resolver governs whether a /v/{slug} link
# loads; the public/listed tags govern what appears on Home/Search. An unlisted
# scene (none of the configured tags) IS reachable by its individual-share slug
# (the capability) but NOT by the global /v/{slug} route.
#
# ``origin`` selects which policy applies:
#   - "global"  → the /v/{slug} route: stash-tag visibility governs; a
#                  VideoOverride.password_hash is ignored so a PUBLIC video can
#                  be viewed globally without knowing its individual-share password.
#   - "override" → the /{slug} individual-share route: only the
#                  VideoOverride.password_hash governs; tag visibility is ignored
#                  (an unlisted scene is reachable via its share slug).
async def resolve_scene_access(request: Request, db: Session, stash_video_id: int,
                               origin: str = "global") -> str:
    """Resolve access to a Stash scene, returning ACCESS_ALLOW /
    ACCESS_PASSWORD_REQUIRED / ACCESS_NOT_FOUND.

    **Global /v/{slug} path** (origin="global"):
      1. hidden tag present → NOT_FOUND.
      2. public / listed tag → ALLOW.
      3. unlisted (no relevant tags) → NOT_FOUND (not reachable statelessly).
      A VideoOverride.password_hash is deliberately NOT checked here: a video
      tagged PUBLIC must play freely in galleries/embeds without prompting.

    **Individual-share /{slug} path** (origin="override"):
      1. Look up the VideoOverride row for the scene.
      2. Row missing → ALLOW (no override => no gate).
      3. Override past expiry → NOT_FOUND.
      4. Override.password_hash set & no valid scene-keyed cookie →
         PASSWORD_REQUIRED.
      5. Otherwise → ALLOW. Stash tag visibility is ignored — an unlisted scene
         is still reachable via its share slug (the slug is the capability).

    The scene's tag set is TTL-cached + single-flight, so this is cheap on the
    hot path. On a transient Stash error the tag set comes back empty (and is not
    cached), so the scene reads as unlisted → NOT_FOUND on the global path (a
    brief 404, not a leak) and ALLOW on the individual-share path.
    """
    sid = int(stash_video_id)

    if origin == "override":
        override = db.query(VideoOverride).filter(VideoOverride.stash_video_id == sid).first()
        if override is None:
            return ACCESS_ALLOW
        if override.expires_at is not None and is_expired(override.expires_at):
            return ACCESS_NOT_FOUND
        if override.password_hash and not has_valid_pw_cookie(request, str(sid)):
            return ACCESS_PASSWORD_REQUIRED
        return ACCESS_ALLOW

    # --- origin == "global" (default) ---
    tag_ids = await get_scene_tag_ids(sid)
    if scene_visibility(tag_ids) == VIS_HIDDEN:
        return ACCESS_NOT_FOUND
    if scene_visibility(tag_ids) not in (VIS_PUBLIC, VIS_LISTED):
        # Unlisted scenes can't be reached by their unguessable /v/{slug} statelessly.
        return ACCESS_NOT_FOUND
    return ACCESS_ALLOW


async def authorize_scene_media(request: Request, stash_video_id: int,
                               via_share_id: str | None = None) -> bool:
    """Gate a media sub-request keyed to a Stash scene id (the /media/{id}/...
    routes) and RETURN whether the media is publicly cacheable. Raises 404
    (hidden/expired) or 403 ("Password required") to follow the media-route
    contract (no human prompt on a sub-request).

    Media routes lack URL-path context (no /v/ vs //{slug}), so they authorize
    from *any* valid capability:
      1. scene carries PUBLIC / LISTED stash tags → ALLOW (public-cacheable).
      2. request carries a valid scene-keyed unlock cookie (set by the
         /{slug} VideoOverride password flow) → ALLOW.
      3. ``via_share_id`` is provided (gallery-scoped route ``/{gallery}/{sqid}``):
         O(1) lookup of that specific SharedTag; if the request carries its
         unlock cookie and the scene is a member → ALLOW.
    Otherwise → 403.

    Returns ``True`` only for public/listed scenes with no password — those bytes
    are identical for everyone and safe for a shared CDN. Unlisted/hidden/
    password-protected → ``False`` (callers send ``private, no-store``).

    Owns a short-lived session for the override + tag-share lookups and CLOSES
    it before the network-bound tag fetch — like :func:`authorize_tag_video` —
    so a burst of segment requests doesn't pin DB connections across that await.
    Cookie-only for passwords (no ?pwd= on media), keyed to the scene id."""
    sid = int(stash_video_id)
    with SessionLocal() as db:
        override = db.query(VideoOverride).filter(VideoOverride.stash_video_id == sid).first()
        ov_expires = override.expires_at if override else None
        ov_password = override.password_hash if override else None
    # ---- session released; no DB connection held past this point ----

    tag_ids = await get_scene_tag_ids(sid)

    # Hidden tag → always 404 regardless of capabilities.
    if VISIBILITY_HIDDEN and VISIBILITY_HIDDEN in tag_ids:
        raise HTTPException(status_code=404, detail="Video not found")

    # Expired override → 404.
    if ov_expires is not None and is_expired(ov_expires):
        raise HTTPException(status_code=404, detail="Video not found")

    # 1. PUBLIC / LISTED stash tag → ALLOW. Public-cacheable only when there's
    #    no per-scene password (the password only gates the /{slug} landing page,
    #    not the bytes themselves).
    level = scene_visibility(tag_ids)
    if level in (VIS_PUBLIC, VIS_LISTED):
        return not ov_password

    # Not publicly visible by stash tag. Check capability cookies:
    # 2. Scene-keyed unlock cookie.  Set by the VideoOverride password flow OR
    #    by visiting an unlisted capability URL that has no password — either way
    #    the browser has proven it can reach the share page, so media is allowed.
    if has_valid_pw_cookie(request, str(sid)):
        return False  # allowed but private (don't CDN-cache gated bytes)

    # 3. Gallery-scoped unlock: the caller (gallery-scoped video route) supplies
    #    the exact share_id via ?via=. O(1) lookup — no scan of all tag shares.
    if via_share_id:
        with SessionLocal() as db:
            tag_share = db.query(SharedTag).filter(SharedTag.share_id == via_share_id).first()
        if tag_share and tag_share.password_hash:
            if has_valid_pw_cookie(request, tag_share.share_id):
                respect_limit = tag_share_respects_limit_tag(tag_share.password_hash,
                                                             tag_share.show_in_gallery,
                                                             tag_share.apply_limit_tag)
                if await is_video_in_tag(tag_share.stash_tag_id, sid,
                                         respect_limit_tag=respect_limit):
                    return False  # allowed, but private (no CDN cache)

    # 4. No capability → 403.
    raise HTTPException(status_code=403, detail="Password required")


def carry_unlock_cookie(request: Request, response, legacy_share_id: str, stash_video_id: int) -> None:
    """When a legacy URL 301s to a canonical /v/ or /media/ URL, re-issue the
    scene-keyed unlock cookie if the request already holds the legacy share's
    unlock cookie — so a viewer who unlocked the old URL stays unlocked.

    No-op when the request doesn't carry the legacy cookie (nothing to carry)."""
    if has_valid_pw_cookie(request, legacy_share_id):
        set_unlock_cookie(response, str(int(stash_video_id)))
