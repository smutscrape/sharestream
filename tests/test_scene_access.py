"""resolve_scene_access: the Phase 2 config-driven visibility gate.

Covers the four visibility outcomes against the configured tag ids, plus the
VideoOverride password/expiry interactions. The scene's tag set (normally fetched
from Stash and TTL-cached) is monkeypatched; VideoOverride rows live in an
in-memory SQLite session.
"""
import datetime
from datetime import timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sharestream.services.access as access_mod
from sharestream.db.models import Base, VideoOverride
from sharestream.services.access import (
    ACCESS_ALLOW,
    ACCESS_NOT_FOUND,
    ACCESS_PASSWORD_REQUIRED,
    authorize_scene_media,
    resolve_scene_access,
)

PUBLIC, LISTED, HIDDEN = "100", "200", "300"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _visibility_config(monkeypatch):
    # Pin the configured visibility tag ids regardless of the dev config.yaml.
    monkeypatch.setattr(access_mod, "VISIBILITY_PUBLIC", PUBLIC)
    monkeypatch.setattr(access_mod, "VISIBILITY_LISTED", LISTED)
    monkeypatch.setattr(access_mod, "VISIBILITY_HIDDEN", HIDDEN)


def _patch_tags(monkeypatch, tag_ids):
    async def fake_get_scene_tag_ids(scene_id):
        return set(tag_ids)
    monkeypatch.setattr(access_mod, "get_scene_tag_ids", fake_get_scene_tag_ids)


async def test_hidden_is_not_found(monkeypatch, db):
    _patch_tags(monkeypatch, {HIDDEN, PUBLIC})  # hidden overrides everything
    assert await resolve_scene_access(None, db, 42) == ACCESS_NOT_FOUND


async def test_public_allows(monkeypatch, db):
    _patch_tags(monkeypatch, {PUBLIC})
    assert await resolve_scene_access(None, db, 42) == ACCESS_ALLOW


async def test_listed_allows(monkeypatch, db):
    _patch_tags(monkeypatch, {LISTED})
    assert await resolve_scene_access(None, db, 42) == ACCESS_ALLOW


async def test_unlisted_not_found_on_global_path(monkeypatch, db):
    """A scene carrying none of the configured tags is unlisted — the unguessable
    slug is the capability, but the /v/{slug} global route must NOT surface
    unlisted scenes statelessly. (They are still reachable via their individual
    /{slug} share URL; see test_override_origin_allows_unlisted.)"""
    _patch_tags(monkeypatch, {"999"})  # some unrelated tag
    assert await resolve_scene_access(None, db, 42) == ACCESS_NOT_FOUND
    _patch_tags(monkeypatch, set())  # no tags at all
    assert await resolve_scene_access(None, db, 42) == ACCESS_NOT_FOUND


async def test_override_origin_allows_unlisted(monkeypatch, db):
    """Unlisted scenes ARE reachable via their individual-share /{slug} URL
    (the slug is the capability), even though stash tag visibility is ignored
    for the override origin."""
    _patch_tags(monkeypatch, {"999"})  # unlisted (no PUBLIC/LISTED/HIDDEN)
    assert await resolve_scene_access(None, db, 42, origin="override") == ACCESS_ALLOW
    _patch_tags(monkeypatch, set())
    assert await resolve_scene_access(None, db, 42, origin="override") == ACCESS_ALLOW


async def test_global_ignores_override_password(monkeypatch, db):
    """A scene tagged PUBLIC plays freely at /v/{slug} even if it has a
    VideoOverride.password elsewhere (that password only gates /{slug})."""
    _patch_tags(monkeypatch, {PUBLIC})
    db.add(VideoOverride(stash_video_id=42, vanity_slug="my-share", password_hash="$2b$dummy"))
    db.commit()
    assert await resolve_scene_access(None, db, 42, origin="global") == ACCESS_ALLOW


async def test_password_required_on_override_path(monkeypatch, db):
    """A scene with a VideoOverride.password_hash prompts on the /{slug}
    individual-share path (origin=override)."""
    _patch_tags(monkeypatch, {PUBLIC})
    db.add(VideoOverride(stash_video_id=42, vanity_slug="gated", password_hash="$2b$dummy"))
    db.commit()
    assert await resolve_scene_access(None, db, 42, origin="override") == ACCESS_PASSWORD_REQUIRED


async def test_expired_override_is_not_found_on_override_path(monkeypatch, db):
    """An expired VideoOverride gates the /{slug} path (NOT_FOUND), but the /v/
    global path doesn't care about override expiry — only stash tags matter."""
    _patch_tags(monkeypatch, {PUBLIC})
    past = datetime.datetime.now(timezone.utc) - datetime.timedelta(days=1)
    db.add(VideoOverride(stash_video_id=42, vanity_slug="expired", expires_at=past))
    db.commit()
    # Override path: expired → NOT_FOUND.
    assert await resolve_scene_access(None, db, 42, origin="override") == ACCESS_NOT_FOUND
    # Global path: expired override is irrelevant; PUBLIC tag → ALLOW.
    assert await resolve_scene_access(None, db, 42, origin="global") == ACCESS_ALLOW


async def test_hidden_beats_password(monkeypatch, db):
    """Hidden wins even over a password-protected scene — 404, not a prompt."""
    _patch_tags(monkeypatch, {HIDDEN})
    db.add(VideoOverride(stash_video_id=42, password_hash="$2b$dummy"))
    db.commit()
    assert await resolve_scene_access(None, db, 42) == ACCESS_NOT_FOUND


# ------------------------------------------------------------------
# authorize_scene_media: capability-without-password (Patch 1 regression)
# ------------------------------------------------------------------
async def test_authorize_scene_media_allows_cookie_without_password(monkeypatch, db):
    """An unlisted scene with NO override password must still be able to load
    media if the browser has the scene-keyed unlock cookie (set by visiting
    the capability URL).  Before the fix, authorize_scene_media only checked
    the cookie when ov_password was truthy, so unlisted+no-password shares
    got 403 on media subrequests."""
    from unittest.mock import MagicMock
    from sharestream.services.access import authorize_scene_media, has_valid_pw_cookie

    _patch_tags(monkeypatch, {"999"})  # unlisted

    # No VideoOverride → no password at all.
    # (override is None, so ov_password = None)

    # Patch has_valid_pw_cookie to return True (simulates the browser having
    # the cookie that was set by visiting the /{slug} page).
    original = has_valid_pw_cookie
    monkeypatch.setattr(access_mod, "has_valid_pw_cookie",
                        lambda req, sid: True)

    try:
        cacheable = await authorize_scene_media(None, 42)
        assert cacheable is False  # allowed, but private (no CDN cache)
    finally:
        monkeypatch.setattr(access_mod, "has_valid_pw_cookie", original)


async def test_authorize_scene_media_403_without_cookie_and_no_password(monkeypatch, db):
    """An unlisted scene with no password and no cookie must still 403 —
    media is only reachable through a capability URL that set the cookie."""
    _patch_tags(monkeypatch, {"999"})  # unlisted

    with pytest.raises(Exception) as exc_info:
        await authorize_scene_media(None, 42)
    assert exc_info.value.status_code == 403
