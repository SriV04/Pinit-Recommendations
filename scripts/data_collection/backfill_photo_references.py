"""
Backfill photo references for locations that don't have one yet.

Supports parallel execution across multiple terminals using sharding.
Each terminal takes a different shard so they never process the same location.

Usage:
    # Single terminal (processes everything):
    python scripts/data_collection/backfill_photo_references.py

    # 3 terminals running in parallel:
    python scripts/data_collection/backfill_photo_references.py --shard 0 --total-shards 3
    python scripts/data_collection/backfill_photo_references.py --shard 1 --total-shards 3
    python scripts/data_collection/backfill_photo_references.py --shard 2 --total-shards 3
"""

import argparse
import os
import time
from typing import Callable, TypeVar

import requests
from dotenv import load_dotenv
from supabase import create_client, Client

T = TypeVar("T")


def retry(fn: Callable[..., T], *args, max_retries: int = 5, base_delay: float = 2.0, **kwargs) -> T:
    """Retry a function with exponential backoff on any exception."""
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            print(f"    ⚠ Attempt {attempt + 1} failed: {e}")
            print(f"    ↳ Retrying in {delay:.0f}s...")
            time.sleep(delay)

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_ANON_KEY"]
GOOGLE_API_KEY = os.environ["GOOGLE_PLACE_API_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

BATCH_SIZE = 500  # How many locations to fetch per round


def _fetch_unprocessed_locations(limit: int = BATCH_SIZE):
    """Fetch locations without a photo_reference, returning location_id and google_place_id."""
    data = (
        supabase.table("locations")
        .select("location_id, google_place_id")
        .is_("photo_reference", "null")
        .not_.is_("google_place_id", "null")
        .order("location_id")
        .limit(limit)
        .execute()
    )
    return data.data or []


def fetch_unprocessed_locations(limit: int = BATCH_SIZE):
    return retry(_fetch_unprocessed_locations, limit)


def get_photo_reference(place_id: str):
    """Get the first photo resource name from Google Places API v1."""
    url = f"https://places.googleapis.com/v1/places/{place_id}"
    headers = {
        "Content-type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": "id,displayName,photos",
    }

    try:
        res = requests.get(url, headers=headers, timeout=10).json()
        photos = res.get("photos", [])
        if not photos:
            return None
        return photos[0].get("name")
    except Exception as e:
        print(f"  Error fetching photo reference for {place_id}: {e}")
        return None


def _update_photo_reference(place_id: str, photo_ref: str):
    """Update the photo_reference column for a location."""
    result = (
        supabase.table("locations")
        .update({"photo_reference": photo_ref})
        .eq("google_place_id", place_id)
        .execute()
    )
    return bool(result.data)


def update_photo_reference(place_id: str, photo_ref: str):
    try:
        return retry(_update_photo_reference, place_id, photo_ref)
    except Exception as e:
        print(f"  Error updating {place_id} after retries: {e}")
        return False


def _mark_no_photo(place_id: str):
    """Mark locations that have no Google photo so we don't re-query them."""
    supabase.table("locations").update(
        {"photo_reference": "none"}
    ).eq("google_place_id", place_id).execute()


def mark_no_photo(place_id: str):
    try:
        retry(_mark_no_photo, place_id)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Backfill photo references for locations.")
    parser.add_argument("--shard", type=int, default=0, help="This worker's shard index (0-based)")
    parser.add_argument("--total-shards", type=int, default=1, help="Total number of parallel workers")
    args = parser.parse_args()

    shard = args.shard
    total_shards = args.total_shards

    if shard >= total_shards or shard < 0:
        print(f"Error: --shard must be between 0 and {total_shards - 1}")
        return

    print(f"Worker shard {shard}/{total_shards}")
    print("=" * 60)

    total_processed = 0
    total_skipped = 0
    total_errors = 0
    round_num = 0

    while True:
        round_num += 1
        locations = fetch_unprocessed_locations()

        if not locations:
            print(f"\nNo more locations to process. Done!")
            break

        # Shard: each worker only takes rows where location_id % total_shards == shard
        my_locations = [
            loc for loc in locations
            if loc["location_id"] % total_shards == shard
        ]

        if not my_locations:
            # Our shard has nothing in this batch, but others might.
            # If the full batch was returned, there may be more rows — keep going.
            if len(locations) < BATCH_SIZE:
                print(f"\nNo more locations for shard {shard}. Done!")
                break
            continue

        print(f"\nRound {round_num}: {len(my_locations)} locations for this shard "
              f"({len(locations)} total unprocessed fetched)")

        for idx, loc in enumerate(my_locations, 1):
            place_id = loc["google_place_id"]
            loc_id = loc["location_id"]
            print(f"  [{idx}/{len(my_locations)}] location_id={loc_id}  {place_id}")

            photo_ref = get_photo_reference(place_id)

            if not photo_ref:
                print(f"    ↳ No photo found, marking as 'none'")
                mark_no_photo(place_id)
                total_skipped += 1
            else:
                success = update_photo_reference(place_id, photo_ref)
                if success:
                    print(f"    ↳ Saved photo reference")
                    total_processed += 1
                else:
                    total_errors += 1

            time.sleep(0.5)

    print(f"\n{'=' * 60}")
    print(f"Worker shard {shard}/{total_shards} finished!")
    print(f"  Processed: {total_processed}")
    print(f"  No photo:  {total_skipped}")
    print(f"  Errors:    {total_errors}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
