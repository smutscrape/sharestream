#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sharestream.config import STASH_API_KEY, STASH_SERVER
from sharestream.db.models import VideoOverride
from sharestream.db.session import SessionLocal
from sharestream.services.slugs import encode_video_id

logger = logging.getLogger("backfill_override_titles")


GRAPHQL_URL = f"{STASH_SERVER}/graphql"


def graphql_headers() -> dict[str, str]:
    return {
        "ApiKey": STASH_API_KEY,
        "Content-Type": "application/json",
    }


def share_label(override: VideoOverride) -> str:
    return override.vanity_slug or encode_video_id(int(override.stash_video_id))


def needs_backfill(override: VideoOverride, force: bool) -> bool:
    if force:
        return True

    current = (override.custom_title or "").strip()
    if not current:
        return True

    if current == f"Scene {int(override.stash_video_id)}":
        return True

    return False


async def fetch_scene_title(client: httpx.AsyncClient, scene_id: int) -> str | None:
    query = {
        "operationName": "FindScene",
        "variables": {"id": str(scene_id)},
        "query": """
            query FindScene($id: ID!) {
                findScene(id: $id) {
                    id
                    title
                    files {
                        basename
                    }
                }
            }
        """,
    }

    try:
        response = await client.post(GRAPHQL_URL, json=query, headers=graphql_headers())
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.warning("scene %s: request failed: %s", scene_id, e)
        return None

    if data.get("errors"):
        logger.warning("scene %s: graphql errors: %s", scene_id, data["errors"])
        return None

    scene = data.get("data", {}).get("findScene")
    if not scene:
        logger.warning("scene %s: not found in Stash", scene_id)
        return None

    title = (scene.get("title") or "").strip()
    if title:
        return title

    basename = (((scene.get("files") or [{}])[0].get("basename")) or "").strip()
    if basename:
        return basename

    return None


async def fetch_titles(scene_ids: list[int], concurrency: int = 12) -> dict[int, str]:
    sem = asyncio.Semaphore(concurrency)
    out: dict[int, str] = {}

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        async def run_one(scene_id: int) -> None:
            async with sem:
                title = await fetch_scene_title(client, scene_id)
                if title:
                    out[scene_id] = title

        await asyncio.gather(*(run_one(scene_id) for scene_id in scene_ids))

    return out


def load_candidates(db: Session, force: bool) -> list[VideoOverride]:
    overrides = db.query(VideoOverride).order_by(VideoOverride.id.asc()).all()
    return [o for o in overrides if needs_backfill(o, force)]


def apply_updates(db: Session, title_map: dict[int, str], dry_run: bool, force: bool) -> tuple[int, int]:
    checked = 0
    updated = 0

    candidates = load_candidates(db, force=force)
    if not candidates:
        return 0, 0

    for override in candidates:
        checked += 1
        scene_id = int(override.stash_video_id)
        new_title = title_map.get(scene_id)
        if not new_title:
            continue

        current = (override.custom_title or "").strip()
        if current == new_title:
            continue

        label = share_label(override)
        print(f"{label}: {current or '(empty)'} -> {new_title}")

        if not dry_run:
            override.custom_title = new_title
            updated += 1

    if not dry_run and updated:
        db.commit()

    return checked, updated


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill VideoOverride.custom_title from Stash scene titles."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change, but do not write to the database.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite all override custom_title values, not just blank/'Scene N' ones.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    with SessionLocal() as db:
        candidates = load_candidates(db, force=args.force)
        scene_ids = [int(o.stash_video_id) for o in candidates]

    if not scene_ids:
        print("No matching overrides found. Nothing to do.")
        return 0

    print(f"Fetching titles for {len(scene_ids)} share override(s) from Stash...")
    title_map = asyncio.run(fetch_titles(scene_ids))

    with SessionLocal() as db:
        checked, updated = apply_updates(
            db,
            title_map=title_map,
            dry_run=args.dry_run,
            force=args.force,
        )

    if args.dry_run:
        print(f"\nDry run complete. Checked {checked} override(s).")
    else:
        print(f"\nDone. Updated {updated} override(s).")

    missing = len(scene_ids) - len(title_map)
    if missing:
        print(f"Skipped {missing} scene(s) because Stash returned no title/basename.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
