#!/usr/bin/env python3
"""
Run the same synchronous pipeline as POST /locations/add (process_synchronously=true)
for one or more Google Place IDs.

Examples:
  python3 scripts/run_locations_add_sync.py ChIJN1t_tDeuEmsRUsoyG83frY4
  python3 scripts/run_locations_add_sync.py --source tiktok --force-menu-vibe ID1 ID2
  python3 scripts/run_locations_add_sync.py --file place_ids.txt
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import uuid4

# Add src to path (repo-local execution, no install required).
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pinit.api.schemas_location_tasks import (  # noqa: E402
    EmojiPayload,
    MenuVibePayload,
    PhotosPayload,
    PipelinePayload,
)
from pinit.api.services.location_tasks import (  # noqa: E402
    emoji_task,
    menu_vibe_task,
    photos_task,
    run_pipeline_inline,
)
from pinit.api.services.proximal_service import (  # noqa: E402
    create_location_from_place_details,
    fetch_google_place_basic_details,
)
from pinit.integrations.supabase import get_supabase_service  # noqa: E402


def _parse_place_ids(args: argparse.Namespace) -> list[str]:
    place_ids: list[str] = []

    if args.file:
        content = Path(args.file).read_text(encoding="utf-8")
        for raw in content.splitlines():
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            place_ids.append(raw)

    if args.google_place_ids:
        for raw in args.google_place_ids:
            for part in raw.split(","):
                part = part.strip()
                if part:
                    place_ids.append(part)

    # De-dupe while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for pid in place_ids:
        if pid in seen:
            continue
        seen.add(pid)
        ordered.append(pid)
    return ordered


async def _run_one_place_id(
    google_place_id: str,
    *,
    source: str,
    generate_emoji: bool,
    classify_photo: bool,
    force_menu_vibe: bool,
) -> int:
    supabase = get_supabase_service()
    request_id = str(uuid4())

    existing_location = await asyncio.to_thread(
        supabase.get_location_by_google_place_id,
        google_place_id,
    )

    created_new = False
    if existing_location:
        location_id = int(existing_location["location_id"])
        name = existing_location.get("name")
    else:
        basic_place_details = await asyncio.to_thread(
            fetch_google_place_basic_details,
            google_place_id,
        )
        if not basic_place_details:
            print(f"[ERROR] {google_place_id}: could not fetch basic details")
            return 1

        created_location = await asyncio.to_thread(
            create_location_from_place_details,
            basic_place_details,
        )
        if not created_location:
            print(f"[ERROR] {google_place_id}: could not create location row")
            return 1

        created_new = True
        location_id = int(created_location["location_id"])
        name = basic_place_details.get("name")

    payload = PipelinePayload(
        task_type="pipeline",
        request_id=request_id,
        location_id=location_id,
        google_place_id=google_place_id,
        source=source,
        generate_emoji=generate_emoji,
        classify_photo=classify_photo,
        created_new=created_new,
    )

    try:
        if force_menu_vibe:
            if payload.generate_emoji:
                await emoji_task(EmojiPayload(task_type="emoji", **payload.model_dump(exclude={"task_type"})))
            if payload.classify_photo:
                await photos_task(PhotosPayload(task_type="photos", **payload.model_dump(exclude={"task_type"})))
            await menu_vibe_task(MenuVibePayload(task_type="menu_vibe", **payload.model_dump(exclude={"task_type"})))
        else:
            await run_pipeline_inline(payload)
    except Exception as exc:
        print(f"[ERROR] {google_place_id} (location_id={location_id}): pipeline failed: {exc}")
        return 1

    updated = await asyncio.to_thread(supabase.get_location, location_id) or {}
    cuisine_primary = updated.get("cuisine_primary")
    updated_vibe = updated.get("updated_vibe")
    print(
        f"[OK] {google_place_id} -> location_id={location_id}"
        f" name={name!r} cuisine_primary={cuisine_primary!r} updated_vibe={updated_vibe!r}"
    )
    return 0


async def main_async() -> int:
    parser = argparse.ArgumentParser(
        description="Run the /locations/add synchronous pipeline for Google Place IDs.",
    )
    parser.add_argument(
        "google_place_ids",
        nargs="*",
        help="One or more Google Place IDs (you can also pass comma-separated values).",
    )
    parser.add_argument(
        "--file",
        help="Path to a newline-separated file of Google Place IDs (lines starting with # are ignored).",
    )
    parser.add_argument(
        "--source",
        default="in-app",
        help="Source string used by the pipeline (default: in-app).",
    )
    parser.add_argument(
        "--no-emoji",
        action="store_true",
        help="Skip emoji generation.",
    )
    parser.add_argument(
        "--no-photo",
        action="store_true",
        help="Skip photo classification / photo pipeline.",
    )
    parser.add_argument(
        "--force-menu-vibe",
        action="store_true",
        help=(
            "Always run emoji/photos/menu+vibe tasks (even if the location already exists "
            "and updated_vibe is true). Useful for backfilling menu+cuisine_primary."
        ),
    )

    args = parser.parse_args()
    place_ids = _parse_place_ids(args)

    if not place_ids:
        parser.error("No Google Place IDs provided (pass args and/or --file).")

    source = (args.source or "in-app").lower().strip()
    generate_emoji = not args.no_emoji
    classify_photo = not args.no_photo

    failures = 0
    for google_place_id in place_ids:
        failures += await _run_one_place_id(
            google_place_id,
            source=source,
            generate_emoji=generate_emoji,
            classify_photo=classify_photo,
            force_menu_vibe=bool(args.force_menu_vibe),
        )

    if failures:
        print(f"Done with failures: {failures}/{len(place_ids)}")
        return 1

    print(f"Done: {len(place_ids)}/{len(place_ids)} succeeded")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()

