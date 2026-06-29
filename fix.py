#!/usr/bin/env python3
"""Fix the duplicated function signatures left by the via_slug patch."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def fix_access_py() -> None:
    path = REPO_ROOT / "sharestream/services/access.py"
    text = path.read_text(encoding="utf-8")

    bad = (
        "async def authorize_scene_media(request: Request, stash_video_id: int,\n"
        "                               via_share_id: str | None = None,\n"
        "async def authorize_scene_media(request: Request, stash_video_id: int,\n"
        "                               via_slug: str | None = None) -> bool:"
    )
    good = (
        "async def authorize_scene_media(request: Request, stash_video_id: int,\n"
        "                               via_share_id: str | None = None,\n"
        "                               via_slug: str | None = None) -> bool:"
    )

    if bad not in text:
        print("  sharestream/services/access.py: already correct or unexpected content")
        return

    path.write_text(text.replace(bad, good, 1), encoding="utf-8")
    print("  sharestream/services/access.py: fixed signature")


def fix_media_proxy_py() -> None:
    path = REPO_ROOT / "sharestream/services/media_proxy.py"
    text = path.read_text(encoding="utf-8")

    bad = (
        "async def generate_m3u8_file(share_id: str, stash_video_id: int, resolution: str,\n"
        "                              via_share_id: str | None = None,\n"
        "async def generate_m3u8_file(share_id: str, stash_video_id: int, resolution: str,\n"
        "                              via_slug: str | None = None) -> bool:"
    )
    good = (
        "async def generate_m3u8_file(share_id: str, stash_video_id: int, resolution: str,\n"
        "                              via_share_id: str | None = None,\n"
        "                              via_slug: str | None = None) -> bool:"
    )

    if bad not in text:
        print("  sharestream/services/media_proxy.py: already correct or unexpected content")
        return

    path.write_text(text.replace(bad, good, 1), encoding="utf-8")
    print("  sharestream/services/media_proxy.py: fixed signature")


def main() -> None:
    print("Fixing duplicated signatures...")
    fix_access_py()
    fix_media_proxy_py()
    print("\nReview with:")
    print("  git diff sharestream/services/access.py sharestream/services/media_proxy.py")


if __name__ == "__main__":
    main()
