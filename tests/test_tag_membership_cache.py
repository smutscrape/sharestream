"""Tag-membership cache: partitioning, the cheap probe, priming, and fallback.

A single Stash tag can be exposed two ways at once: a public share (limit_to_tag
applied -> filtered set) and a password-protected share (limit_to_tag bypassed ->
full set). Every cache here keys on (tag_id, respect_limit_tag) so the two never
clobber one another — otherwise a public share could serve un-curated videos, or
a private share could 404 valid ones.

is_video_in_tag resolves a lone video via a cheap single-scene probe
(tag_contains_scene) rather than listing the whole tag; a primed id set short-
circuits the probe; and a probe error falls back to the full id set.
"""
import pytest

import sharestream.services.cache as cache_mod
from sharestream.services.cache import (
    clear_tag_membership_cache,
    is_video_in_tag,
    prime_tag_membership,
)


def _fake_probe():
    """Fake tag_contains_scene: filtered view contains only video 1; the full
    (limit-bypassed) view also contains video 2. Records every upstream call."""
    calls = []

    async def fake_tag_contains_scene(tag_id, video_id, respect_limit_tag=True):
        calls.append((str(tag_id), int(video_id), respect_limit_tag))
        members = {1} if respect_limit_tag else {1, 2}
        return int(video_id) in members

    return fake_tag_contains_scene, calls


async def test_probe_partitions_on_respect_limit_tag(monkeypatch):
    fake, calls = _fake_probe()
    monkeypatch.setattr(cache_mod, "tag_contains_scene", fake)
    clear_tag_membership_cache()
    try:
        # Public (limit_to_tag applied): video 2 is NOT a member.
        assert await is_video_in_tag("7", 2, respect_limit_tag=True) is False
        # Password-protected (filter bypassed): video 2 IS a member.
        assert await is_video_in_tag("7", 2, respect_limit_tag=False) is True

        # Each partition probed exactly once.
        assert sorted(calls) == [("7", 2, False), ("7", 2, True)]

        # Repeat both lookups: same answers, NO new probes (per-video cached).
        assert await is_video_in_tag("7", 2, respect_limit_tag=True) is False
        assert await is_video_in_tag("7", 2, respect_limit_tag=False) is True
        assert sorted(calls) == [("7", 2, False), ("7", 2, True)]
    finally:
        clear_tag_membership_cache()


async def test_primed_set_short_circuits_probe(monkeypatch):
    """A gallery that primed a tag's full id set must serve membership from it
    without ever probing Stash."""
    probed = []

    async def boom(tag_id, video_id, respect_limit_tag=True):
        probed.append((str(tag_id), int(video_id)))
        raise AssertionError("probe should not be called when the set is primed")

    monkeypatch.setattr(cache_mod, "tag_contains_scene", boom)
    clear_tag_membership_cache()
    try:
        prime_tag_membership("9", [1, 2, 3], respect_limit_tag=True)
        assert await is_video_in_tag("9", 2, respect_limit_tag=True) is True
        assert await is_video_in_tag("9", 5, respect_limit_tag=True) is False
        assert probed == []
        # A DIFFERENT partition isn't primed, so it would probe — confirm the
        # priming was partition-scoped, not global.
        with pytest.raises(AssertionError):
            await is_video_in_tag("9", 2, respect_limit_tag=False)
    finally:
        clear_tag_membership_cache()


async def test_probe_error_falls_back_to_id_set(monkeypatch):
    """If the probe errors (None — e.g. an older Stash without scene-id filters),
    membership falls back to the full id set so correctness is preserved."""
    async def probe_unavailable(tag_id, video_id, respect_limit_tag=True):
        return None

    id_set_calls = []

    async def fake_get_tag_scene_ids(tag_id, respect_limit_tag=True):
        id_set_calls.append((str(tag_id), respect_limit_tag))
        return {1, 2}

    monkeypatch.setattr(cache_mod, "tag_contains_scene", probe_unavailable)
    monkeypatch.setattr(cache_mod, "get_tag_scene_ids", fake_get_tag_scene_ids)
    clear_tag_membership_cache()
    try:
        assert await is_video_in_tag("11", 2, respect_limit_tag=True) is True
        assert await is_video_in_tag("11", 9, respect_limit_tag=True) is False
        # The id set was fetched (and cached), so the second lookup reused it.
        assert id_set_calls == [("11", True)]
    finally:
        clear_tag_membership_cache()
