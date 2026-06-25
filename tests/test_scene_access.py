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


async def test_unlisted_allows(monkeypatch, db):
    """A scene carrying none of the configured tags is unlisted — the unguessable
    slug is the capability, so a direct /v/ link ALLOWs."""
    _patch_tags(monkeypatch, {"999"})  # some unrelated tag
    assert await resolve_scene_access(None, db, 42) == ACCESS_ALLOW
    _patch_tags(monkeypatch, set())  # no tags at all
    assert await resolve_scene_access(None, db, 42) == ACCESS_ALLOW


async def test_password_required_without_cookie(monkeypatch, db):
    _patch_tags(monkeypatch, {PUBLIC})
    db.add(VideoOverride(stash_video_id=42, password_hash="$2b$dummy"))
    db.commit()
    # request=None → no cookie → must prompt even though the scene is public.
    assert await resolve_scene_access(None, db, 42) == ACCESS_PASSWORD_REQUIRED


async def test_expired_override_is_not_found(monkeypatch, db):
    _patch_tags(monkeypatch, {PUBLIC})
    past = datetime.datetime.now(timezone.utc) - datetime.timedelta(days=1)
    db.add(VideoOverride(stash_video_id=42, expires_at=past))
    db.commit()
    assert await resolve_scene_access(None, db, 42) == ACCESS_NOT_FOUND


async def test_hidden_beats_password(monkeypatch, db):
    """Hidden wins even over a password-protected scene — 404, not a prompt."""
    _patch_tags(monkeypatch, {HIDDEN})
    db.add(VideoOverride(stash_video_id=42, password_hash="$2b$dummy"))
    db.commit()
    assert await resolve_scene_access(None, db, 42) == ACCESS_NOT_FOUND
