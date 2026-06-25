"""Sqids video-slug encode/decode: round-trip, garbage rejection, and the
canonical re-encode guard that prevents one scene answering at many URLs."""
import pytest

from sharestream.services.slugs import decode_video_id, encode_video_id


@pytest.mark.parametrize("sid", [1, 2, 3, 42, 1042, 99999, 1_000_000])
def test_round_trip(sid):
    assert decode_video_id(encode_video_id(sid)) == sid


def test_empty_and_garbage_decode_to_none():
    assert decode_video_id("") is None
    assert decode_video_id("!!!") is None
    assert decode_video_id("a") is None


def test_non_canonical_alias_rejected():
    """Sqids can decode a padded string to the same number; our decode must
    reject anything that doesn't re-encode to the exact input, so a scene has
    exactly one canonical slug. The canonical slug itself still decodes."""
    canonical = encode_video_id(1042)
    assert decode_video_id(canonical) == 1042
    # A trailing-char alias is a distinct, NON-canonical string. It must resolve
    # to None — never silently to 1042 — so it can't shadow the canonical URL.
    assert decode_video_id(canonical + "a") is None


def test_adjacent_guess_misses():
    canonical = encode_video_id(1042)
    flipped = canonical[:-1] + ("a" if canonical[-1] != "a" else "b")
    decoded = decode_video_id(flipped)
    # Either undecodable, or decodes to a different id — never silently 1042.
    assert decoded != 1042
