"""Regression: rendered HTML must never expose raw numeric Stash scene ids.

All media, embed, and Open Graph URLs must use the Sqids hashid — never
a bare /media/12345/... or /embed/12345 path.  A numeric id leaking into
the HTML would let crawlers or viewers guess adjacent scenes.
"""
import re

import pytest

from sharestream.core.templates import render
from sharestream.services.slugs import decode_video_id, encode_video_id


# A numeric-id path looks like /media/12345/…  (one or more digits after
# /media/).  The hashids produced by Sqids are alphanumeric and never pure
# digits for reasonable input ids, so anything matching this pattern is a leak.
_NUMERIC_MEDIA_RE = re.compile(r"/media/\d+/")
_NUMERIC_EMBED_RE = re.compile(r"/embed/\d+")


def _render_player_html(sqid: str, base_domain: str = "https://example.com") -> str:
    """Render video-player.html with a minimal context sufficient to produce
    the URLs we want to check.  Missing keys get Jinja's default (empty string
    or undefined — we provide the essentials so the template doesn't error)."""
    return render(
        "video-player.html",
        hashid=sqid,
        video_name="Test Video",
        video_details={},
        embed_video_url=f"{base_domain}/media/{sqid}/stream.mp4",
        hit_count=0,
        canonical_url=f"{base_domain}/v/{sqid}",
        verify_action=f"/v/{sqid}/verify",
        # site_context fields (minimal stub so the template renders)
        logo_path="/static/logo.svg",
        srcset="",
        site_name="Teststream",
        site_motto="",
        site_description="",
        og_site_image="",
        social_links=[],
        base_domain=base_domain,
        disclaimer="",
        footer_action_links=[],
        footer_inline_links=[],
        show_content_warning=False,
        content_warning="",
    )


@pytest.mark.parametrize("scene_id", [1, 42, 999, 10_000])
def test_no_numeric_media_ids_in_player_html(scene_id):
    """Rendering video-player.html must never produce /media/<digits>/ URLs."""
    sqid = encode_video_id(scene_id)
    html = _render_player_html(sqid)

    # 1. No bare-numeric /media/<digits>/ paths anywhere in the document.
    assert not _NUMERIC_MEDIA_RE.search(html), (
        f"Numeric media id leaked into HTML for scene {scene_id} (sqid={sqid})"
    )

    # 2. No bare-numeric /embed/<digits> paths.
    assert not _NUMERIC_EMBED_RE.search(html), (
        f"Numeric embed id leaked into HTML for scene {scene_id} (sqid={sqid})"
    )

    # 3. The sqid MUST appear in the output (sanity: the template actually uses it).
    assert f"/media/{sqid}/" in html, (
        f"Expected /media/{sqid}/ not found in HTML for scene {scene_id}"
    )

    # 4. og:url should use the canonical URL, not a numeric path.
    og_url_match = re.search(r'property="og:url"\s+content="([^"]+)"', html)
    assert og_url_match, "og:url meta tag not found"
    og_url = og_url_match.group(1)
    assert not re.search(r"/media/\d+/", og_url), (
        f"og:url contains numeric media path: {og_url}"
    )


def test_hashid_is_not_pure_digits():
    """A Sqids encoding must not be pure digits — that would look like a raw id."""
    for sid in [1, 42, 999, 10_000]:
        sqid = encode_video_id(sid)
        assert not sqid.isdigit(), (
            f"encode_video_id({sid}) = '{sqid}' is pure digits — "
            "hashid must be alphanumeric"
        )


def test_embed_html_uses_hashid():
    """video-embed.html must use the hashid (not a numeric id) for media paths."""
    sqid = encode_video_id(42)
    html = render("video-embed.html", hashid=sqid, video_name="Test Video")
    assert f"/media/{sqid}/" in html
    assert not _NUMERIC_MEDIA_RE.search(html), (
        f"Numeric media id leaked into embed HTML (sqid={sqid})"
    )
