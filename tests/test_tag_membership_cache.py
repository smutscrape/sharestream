"""Tag-membership cache partitioning.

The cache must key on (tag_id, respect_limit_tag). A single Stash tag can be
exposed two ways at once: a public share (limit_to_tag applied -> filtered set)
and a password-protected share (limit_to_tag bypassed -> full set). If the cache
keyed on tag_id alone, whichever request warmed it first would leak its set to
the other — a public share could serve un-curated videos, or a private share
could 404 valid ones. This test pins that partitioning down.
"""
import pytest

import sharestream.services.cache as cache_mod
from sharestream.services.cache import clear_tag_membership_cache, is_video_in_tag


def _fake_stash():
    """Fake get_all_videos_by_tag: filtered view has only video 1; the full
    (limit-bypassed) view also has video 2. Records every upstream call."""
    calls = []

    async def fake_get_all_videos_by_tag(tag_id, respect_limit_tag=True):
        calls.append((str(tag_id), respect_limit_tag))
        return [{"id": 1}] if respect_limit_tag else [{"id": 1}, {"id": 2}]

    return fake_get_all_videos_by_tag, calls


async def test_cache_partitions_on_respect_limit_tag(monkeypatch):
    fake, calls = _fake_stash()
    monkeypatch.setattr(cache_mod, "get_all_videos_by_tag", fake)
    clear_tag_membership_cache()
    try:
        # Public (limit_to_tag applied): video 2 is NOT a member.
        assert await is_video_in_tag("7", 2, respect_limit_tag=True) is False
        # Password-protected (filter bypassed): video 2 IS a member.
        assert await is_video_in_tag("7", 2, respect_limit_tag=False) is True

        # Each partition fetched from Stash exactly once.
        assert sorted(calls) == [("7", False), ("7", True)]

        # Repeat both lookups: same answers, and NO new upstream calls (both
        # partitions are now served from their own cache entry).
        assert await is_video_in_tag("7", 2, respect_limit_tag=True) is False
        assert await is_video_in_tag("7", 2, respect_limit_tag=False) is True
        assert sorted(calls) == [("7", False), ("7", True)]
    finally:
        clear_tag_membership_cache()
