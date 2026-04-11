"""
Backfill emojis for restaurant locations that don't have one yet.

Supports parallel execution across multiple terminals using sharding.
Each terminal takes a different shard so they never process the same location.

Usage:
    # Single terminal (processes everything):
    python scripts/data_collection/backfill_emojis.py

    # 3 terminals running in parallel:
    python scripts/data_collection/backfill_emojis.py --shard 0 --total-shards 3
    python scripts/data_collection/backfill_emojis.py --shard 1 --total-shards 3
    python scripts/data_collection/backfill_emojis.py --shard 2 --total-shards 3
"""

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, TypeVar

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

# Add src to path so we can import pinit modules
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pinit.api.services.proximal_service import add_location_emoji

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_ANON_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

BATCH_SIZE = 500

# Food-related types from Google Places API
FOOD_TYPES = {
    "restaurant", "cafe", "bar", "food",
    "meal_delivery", "meal_takeaway", "bakery", "night_club",
}


def _fetch_locations_without_emoji(limit: int = BATCH_SIZE) -> List[Dict[str, Any]]:
    """Fetch locations without an emoji, returning fields needed for generation."""
    data = (
        supabase.table("locations")
        .select("location_id, name, types, rating, price_level, editorial_summary, vicinity")
        .is_("emoji", "null")
        .order("location_id")
        .limit(limit)
        .execute()
    )
    return data.data or []


def fetch_locations_without_emoji(limit: int = BATCH_SIZE) -> List[Dict[str, Any]]:
    return retry(_fetch_locations_without_emoji, limit)


def is_restaurant_location(location: Dict[str, Any]) -> bool:
    """Check if location is a food establishment based on its types."""
    types = location.get("types", "")
    if not types:
        return False
    if isinstance(types, str):
        types_list = [t.strip().lower() for t in types.split(",") if t.strip()]
    else:
        types_list = types
    return bool(set(types_list) & FOOD_TYPES)


def location_to_place_details(location: Dict[str, Any]) -> Dict[str, Any]:
    """Convert DB location record to the format expected by add_location_emoji."""
    types = location.get("types", "")
    if isinstance(types, str):
        types_list = [t.strip() for t in types.split(",") if t.strip()]
    else:
        types_list = types or []

    return {
        "name": location.get("name", "Unknown"),
        "types": types_list,
        "price_level": location.get("price_level"),
        "rating": location.get("rating"),
        "editorial_summary": {"overview": location.get("editorial_summary", "")},
        "vicinity": location.get("vicinity", ""),
        "reviews": [],
    }


def _update_emoji(location_id: int, emoji: str) -> bool:
    """Update the emoji column for a location."""
    result = (
        supabase.table("locations")
        .update({"emoji": emoji})
        .eq("location_id", location_id)
        .execute()
    )
    return bool(result.data)


def update_emoji(location_id: int, emoji: str) -> bool:
    try:
        return retry(_update_emoji, location_id, emoji)
    except Exception as e:
        print(f"  Error updating location {location_id} after retries: {e}")
        return False


def _mark_no_emoji(location_id: int):
    """Mark locations where emoji generation failed so we don't re-query them."""
    supabase.table("locations").update(
        {"emoji": "none"}
    ).eq("location_id", location_id).execute()


def mark_no_emoji(location_id: int):
    try:
        retry(_mark_no_emoji, location_id)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Backfill emojis for locations.")
    parser.add_argument("--shard", type=int, default=0, help="This worker's shard index (0-based)")
    parser.add_argument("--total-shards", type=int, default=1, help="Total number of parallel workers")
    args = parser.parse_args()

    shard = args.shard
    total_shards = args.total_shards

    if shard >= total_shards or shard < 0:
        print(f"Error: --shard must be between 0 and {total_shards - 1}")
        return

    # Check for OpenAI API key
    if not os.getenv("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY environment variable not found!")
        sys.exit(1)

    print(f"Worker shard {shard}/{total_shards}")
    print("=" * 60)

    total_processed = 0
    total_skipped = 0
    total_not_restaurant = 0
    total_errors = 0
    round_num = 0

    while True:
        round_num += 1
        locations = fetch_locations_without_emoji()

        if not locations:
            print(f"\nNo more locations to process. Done!")
            break

        # Shard: each worker only takes rows where location_id % total_shards == shard
        my_locations = [
            loc for loc in locations
            if loc["location_id"] % total_shards == shard
        ]

        if not my_locations:
            if len(locations) < BATCH_SIZE:
                print(f"\nNo more locations for shard {shard}. Done!")
                break
            continue

        print(f"\nRound {round_num}: {len(my_locations)} locations for this shard "
              f"({len(locations)} total unprocessed fetched)")

        for idx, loc in enumerate(my_locations, 1):
            location_id = loc["location_id"]
            name = loc.get("name", "Unknown")
            print(f"  [{idx}/{len(my_locations)}] location_id={location_id}  {name}")

            # Skip non-restaurant locations
            if not is_restaurant_location(loc):
                print(f"    ↳ Not a restaurant type, marking as 'none'")
                mark_no_emoji(location_id)
                total_not_restaurant += 1
                continue

            # Generate emoji
            try:
                place_details = location_to_place_details(loc)
                emoji = add_location_emoji(place_details)
            except Exception as e:
                print(f"    ↳ Error generating emoji: {e}")
                total_errors += 1
                continue

            if emoji:
                success = update_emoji(location_id, emoji)
                if success:
                    print(f"    ↳ Saved emoji: {emoji}")
                    total_processed += 1
                else:
                    total_errors += 1
            else:
                print(f"    ↳ Generation returned None, marking as 'none'")
                mark_no_emoji(location_id)
                total_skipped += 1

            time.sleep(0.5)

    print(f"\n{'=' * 60}")
    print(f"Worker shard {shard}/{total_shards} finished!")
    print(f"  Processed:      {total_processed}")
    print(f"  Not restaurant: {total_not_restaurant}")
    print(f"  No emoji:       {total_skipped}")
    print(f"  Errors:         {total_errors}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
