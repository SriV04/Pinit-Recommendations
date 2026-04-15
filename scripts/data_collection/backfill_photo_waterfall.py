"""
Backfill photos for locations in location_popularity_app using the
og:image → Google waterfall pipeline.

Usage:
    # Normal run:
    python scripts/data_collection/backfill_photo_waterfall.py

    # Dry run (preview only):
    python scripts/data_collection/backfill_photo_waterfall.py --dry-run

    # Preview first 10 images (saves to /tmp/photo_preview/, opens Finder):
    python scripts/data_collection/backfill_photo_waterfall.py --preview

    # Preview more:
    python scripts/data_collection/backfill_photo_waterfall.py --preview --preview-count 20
"""

import argparse
import os
import sys
import time

# Allow imports from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import requests
from dotenv import load_dotenv
from supabase import create_client, Client

from pinit.api.services.photo_pipeline import fetch_photo_waterfall

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_ANON_KEY"]
GOOGLE_API_KEY = os.environ["GOOGLE_PLACE_API_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

BATCH_SIZE = 200


def fetch_locations_to_process(limit: int = BATCH_SIZE):
    """Fetch locations from location_popularity_app that need photos."""
    # Inner join via foreign key: only locations users have interacted with
    data = (
        supabase.table("location_popularity_app")
        .select("location_id, locations(location_id, google_place_id, website, name)")
        .order("location_id")
        .limit(limit)
        .execute()
    )
    results = []
    for row in data.data or []:
        loc = row.get("locations")
        if not loc:
            continue
        # Skip if already has a stored image
        # (We can't filter image_stored in the join easily, so filter here)
        results.append(loc)
    return results


def fetch_locations_needing_photos(limit: int = BATCH_SIZE):
    """Fetch popularity locations that don't yet have image_stored=True."""
    # Use a direct query on locations with an inner join filter
    data = (
        supabase.table("locations")
        .select("location_id, google_place_id, website, name, image_stored, photo_source")
        .in_(
            "location_id",
            [
                r["location_id"]
                for r in (
                    supabase.table("location_popularity_app")
                    .select("location_id")
                    .order("location_id")
                    .limit(limit * 2)  # fetch extra to account for already-processed ones
                    .execute()
                ).data or []
            ],
        )
        .is_("image_stored", "false")
        .not_.is_("google_place_id", "null")
        .order("location_id")
        .limit(limit)
        .execute()
    )
    return data.data or []


def upload_photo(location_id: int, image_bytes: bytes, content_type: str) -> bool:
    """Upload photo to Supabase storage bucket."""
    ext_map = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    ext = ext_map.get(content_type, ".jpg")
    object_name = f"{location_id}{ext}"

    try:
        supabase.storage.from_("location_photos").upload(
            object_name,
            image_bytes,
            {"content-type": content_type, "upsert": "true"},
        )
        return True
    except Exception as e:
        print(f"    ⚠ Storage upload failed: {e}")
        return False


def update_location(location_id: int, **kwargs) -> bool:
    """Update location record."""
    try:
        supabase.table("locations").update(kwargs).eq("location_id", location_id).execute()
        return True
    except Exception as e:
        print(f"    ⚠ DB update failed for {location_id}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Backfill photos via og:image → Google waterfall.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--preview", action="store_true", help="Download images to /tmp/photo_preview/ for inspection")
    parser.add_argument("--preview-count", type=int, default=10, help="Number of images to preview (default 10)")
    args = parser.parse_args()

    mode = "PREVIEW" if args.preview else ("DRY RUN" if args.dry_run else "LIVE")
    print("Photo Waterfall Backfill")
    print(f"  Scope: location_popularity_app locations")
    print(f"  Mode:  {mode}")
    if args.preview:
        print(f"  Preview count: {args.preview_count}")
    print("=" * 60)

    # --- PREVIEW MODE ---
    if args.preview:
        import subprocess

        preview_dir = "/tmp/photo_preview"
        os.makedirs(preview_dir, exist_ok=True)
        # Clear old previews
        for f in os.listdir(preview_dir):
            os.remove(os.path.join(preview_dir, f))

        locations = fetch_locations_needing_photos(limit=args.preview_count * 2)
        # Prefer locations with websites so we actually test og:image
        locations.sort(key=lambda l: (0 if l.get("website") else 1))
        locations = locations[:args.preview_count]

        found = 0
        for idx, loc in enumerate(locations, 1):
            loc_id = loc["location_id"]
            name = loc.get("name", "?")
            website = loc.get("website")

            print(f"  [{idx}/{len(locations)}] #{loc_id} {name}")
            print(f"    website: {website or '(none)'}")

            location_row = {
                "website": website,
                "google_place_id": loc.get("google_place_id"),
                "name": name,
            }

            result = fetch_photo_waterfall(location_row, GOOGLE_API_KEY)
            if result:
                ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
                ext = ext_map.get(result.content_type, ".jpg")
                # Sanitize name for filename
                safe_name = "".join(c if c.isalnum() or c in " -_" else "" for c in name).strip().replace(" ", "_")
                filename = f"{loc_id}_{safe_name}_{result.source}{ext}"
                filepath = os.path.join(preview_dir, filename)
                with open(filepath, "wb") as f:
                    f.write(result.image_bytes)
                print(f"    → {result.source} | {len(result.image_bytes):,} bytes | saved: {filename}")
                found += 1
            else:
                print(f"    → no photo from any source")

            time.sleep(0.3)

        print(f"\n{'=' * 60}")
        print(f"Saved {found} images to {preview_dir}/")
        print(f"{'=' * 60}")

        # Open folder in Finder
        subprocess.run(["open", preview_dir])
        return

    # --- DRY RUN / LIVE MODE ---
    stats = {"og_image": 0, "google": 0, "none": 0, "errors": 0, "skipped": 0}
    round_num = 0

    while True:
        round_num += 1
        locations = fetch_locations_needing_photos()

        if not locations:
            print(f"\nNo more locations to process. Done!")
            break

        print(f"\nRound {round_num}: {len(locations)} locations to process")

        for idx, loc in enumerate(locations, 1):
            loc_id = loc["location_id"]
            place_id = loc.get("google_place_id")
            website = loc.get("website")
            name = loc.get("name", "?")

            print(f"  [{idx}/{len(locations)}] #{loc_id} {name}")
            print(f"    website: {website or '(none)'}")

            if args.dry_run:
                print(f"    → [DRY RUN] would attempt og:image then google")
                stats["skipped"] += 1
                continue

            location_row = {
                "website": website,
                "google_place_id": place_id,
                "name": name,
            }

            result = fetch_photo_waterfall(location_row, GOOGLE_API_KEY)

            if result:
                print(f"    → source: {result.source} ({len(result.image_bytes)} bytes, score={result.score})")

                uploaded = upload_photo(loc_id, result.image_bytes, result.content_type)
                if uploaded:
                    update_data = {
                        "photo_reference": result.source_reference,
                        "photo_source": result.source,
                        "image_stored": True,
                    }
                    if result.score is not None:
                        update_data["photo_reference_score"] = str(result.score)
                    update_location(loc_id, **update_data)
                    stats[result.source] += 1
                else:
                    stats["errors"] += 1
            else:
                print(f"    → no photo from any source")
                update_location(loc_id, photo_source="none")
                stats["none"] += 1

            time.sleep(0.5)

    print(f"\n{'=' * 60}")
    print("Results:")
    print(f"  og:image: {stats['og_image']}")
    print(f"  google:   {stats['google']}")
    print(f"  none:     {stats['none']}")
    print(f"  errors:   {stats['errors']}")
    if args.dry_run:
        print(f"  skipped:  {stats['skipped']} (dry run)")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
