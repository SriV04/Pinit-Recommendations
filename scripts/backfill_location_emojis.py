#!/usr/bin/env python3
"""
Backfill script to generate emojis for existing restaurant locations.

This script will:
1. Load all restaurant locations from Supabase
2. Filter for locations without emojis
3. Generate emojis using OpenAI
4. Update the database with the results
"""

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

# Add src to path so we can import pinit modules
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pinit.api.services.proximal_service import add_location_emoji
from pinit.integrations.supabase import get_supabase_service

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("backfill_emojis.log"),
    ],
)
logger = logging.getLogger(__name__)

# Food-related types from Google Places API
FOOD_TYPES = {
    "restaurant",
    "cafe",
    "bar",
    "food",
    "meal_delivery",
    "meal_takeaway",
    "bakery",
    "night_club",
}


def is_restaurant_location(location: Dict[str, Any]) -> bool:
    """Check if location is a food establishment based on its types.

    Args:
        location: Location record from database

    Returns:
        True if location is food-related, False otherwise
    """
    types = location.get("types", "")

    # Handle empty or None types
    if not types:
        return False

    # Parse types (stored as comma-separated string)
    if isinstance(types, str):
        types_list = [t.strip().lower() for t in types.split(",") if t.strip()]
    else:
        types_list = types

    # Check if any type matches food types
    return bool(set(types_list) & FOOD_TYPES)


def location_to_place_details(location: Dict[str, Any]) -> Dict[str, Any]:
    """Convert database location record to place_details format expected by add_location_emoji.

    Args:
        location: Location record from database

    Returns:
        Dictionary with: name, types, rating, price_level, editorial_summary, vicinity, reviews
    """
    # Parse types
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
        "editorial_summary": {
            "overview": location.get("editorial_summary", "")
        },
        "vicinity": location.get("vicinity", ""),
        "reviews": [],  # No reviews stored in database
    }


def main():
    """Main backfill function."""
    logger.info("=" * 80)
    logger.info("Starting emoji backfill for existing restaurant locations")
    logger.info("=" * 80)

    # Check for OpenAI API key
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        logger.error("OPENAI_API_KEY environment variable not found!")
        logger.error("Please set it before running this script.")
        sys.exit(1)

    logger.info("✓ OPENAI_API_KEY found")

    # Initialize Supabase
    try:
        supabase = get_supabase_service()
        logger.info("✓ Connected to Supabase")
    except Exception as exc:
        logger.error("Failed to connect to Supabase: %s", exc, exc_info=True)
        sys.exit(1)

    # Load all locations
    logger.info("Loading all locations from database...")
    try:
        all_locations = supabase.get_location_without_emoji()
        logger.info("✓ Loaded %d total locations", len(all_locations))
    except Exception as exc:
        logger.error("Failed to load locations: %s", exc, exc_info=True)
        sys.exit(1)

    # Filter for restaurants without emojis
    logger.info("Filtering for restaurants without emojis...")
    restaurants_without_emoji: List[Dict[str, Any]] = []

    for location in all_locations:
        # Skip if already has emoji
        if location.get("emoji"):
            continue

        # Skip if not a restaurant
        if not is_restaurant_location(location):
            continue

        restaurants_without_emoji.append(location)

    logger.info("✓ Found %d restaurants without emojis", len(restaurants_without_emoji))

    if not restaurants_without_emoji:
        logger.info("No restaurants need emoji generation. Exiting.")
        return

    # Process each restaurant
    logger.info("=" * 80)
    logger.info("Starting emoji generation...")
    logger.info("=" * 80)

    success_count = 0
    failure_count = 0
    skipped_count = 0

    for idx, location in enumerate(restaurants_without_emoji, 1):
        location_id = location["location_id"]
        location_name = location.get("name", "Unknown")

        logger.info(
            "[%d/%d] Processing: %s (ID: %s)",
            idx,
            len(restaurants_without_emoji),
            location_name,
            location_id,
        )

        try:
            # Convert to place_details format
            place_details = location_to_place_details(location)

            # Generate emoji
            emoji = add_location_emoji(place_details)

            if emoji:
                # Update database
                supabase.update_location(location_id, emoji=emoji)
                logger.info("  ✓ Generated and saved emoji: %s", emoji)
                success_count += 1
            else:
                logger.warning("  ⚠ Failed to generate emoji (returned None)")
                failure_count += 1

        except Exception as exc:
            logger.error(
                "  ✗ Error processing location %s: %s",
                location_id,
                exc,
                exc_info=True,
            )
            failure_count += 1

        # Rate limiting: 1 second delay between requests to avoid hitting OpenAI limits
        if idx < len(restaurants_without_emoji):
            time.sleep(1)

    # Print summary
    logger.info("=" * 80)
    logger.info("Backfill completed!")
    logger.info("=" * 80)
    logger.info("Total restaurants processed: %d", len(restaurants_without_emoji))
    logger.info("Successfully generated emojis: %d", success_count)
    logger.info("Failed to generate emojis: %d", failure_count)
    logger.info("=" * 80)

    if success_count > 0:
        logger.info("✓ Backfill completed successfully!")
    else:
        logger.warning("⚠ No emojis were generated.")


if __name__ == "__main__":
    main()
