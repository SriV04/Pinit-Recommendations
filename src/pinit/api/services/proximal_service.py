from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from openai import OpenAI

from pinit.config.secrets import GOOGLE_MAPS_API_KEY
from pinit.config.settings import ReviewTagConfig
from pinit.core.recommendation.static_tagging import build_location_tags
from pinit.core.recommendation.tag_taxonomy import get_tags_dataframe
from pinit.core.recommendation.user_profiles import (
    convert_supabase_affinities_to_profile_format,
    load_user_tag_affinities_from_supabase,
)
from pinit.integrations.supabase import get_supabase_service

logger = logging.getLogger(__name__)

DATA_CACHE: Dict[str, Any] = {
    "locations": None,
    "tags": None,
    "location_tags": None,
    "user_tags": None,
    "user_history": None,
    "loaded": False,
}


def get_taste_breakdown(
    location_id: int,
    user_id: str,
    user_tags: pd.DataFrame,
    location_tags: pd.DataFrame,
    top_n: int = 5,
) -> List[Dict[str, Any]]:
    """
    Get the breakdown of which tags contributed to the taste score.

    Returns:
        List of dicts with tag, user_score, location_score, contribution
    """
    user_profile = user_tags[user_tags["user_id"] == user_id]
    loc_tags = location_tags[location_tags["location_id"] == location_id]

    if user_profile.empty or loc_tags.empty:
        return []

    # Merge to find matching tags
    merged = user_profile.merge(
        loc_tags[["tag_id", "tag_text", "score"]],
        on="tag_id",
        how="inner",
        suffixes=("_user", "_loc"),
    )

    if merged.empty:
        return []

    # Calculate contribution
    merged["contribution"] = (merged["score_user"] / 100.0) * (merged["score_loc"] / 100.0)
    merged = merged.sort_values("contribution", ascending=False)

    results: List[Dict[str, Any]] = []
    for _, row in merged.head(top_n).iterrows():
        # Use either tag_text_user or tag_text_loc (they should be the same)
        tag_name = row.get("tag_text_user", row.get("tag_text_loc", "unknown"))
        results.append(
            {
                "tag": tag_name,
                "user_score": float(row["score_user"]),
                "location_score": float(row["score_loc"]),
                "contribution": float(row["contribution"] * 100),  # Convert to 0-100 scale
            }
        )

    return results


def load_data() -> None:
    """Load all necessary data for recommendations from Supabase."""
    if DATA_CACHE["loaded"]:
        logger.info("Data already loaded, skipping reload")
        return

    logger.info("Starting to load recommendation data from Supabase...")

    # Get Supabase client
    supabase = get_supabase_service()

    # Load tags from Supabase
    logger.info("Loading tags from Supabase...")
    tags_df = get_tags_dataframe()
    logger.info("Loaded %d tags", len(tags_df))

    # Load locations from Supabase
    logger.info("Loading locations from Supabase...")
    locations_data = supabase.get_locations(limit=1_000_000)
    if not locations_data:
        logger.error("No locations found in Supabase")
        raise ValueError("No locations found in Supabase database")

    locations = pd.DataFrame(locations_data)
    logger.info("Loaded %d locations", len(locations))

    # Load location_tags from Supabase
    logger.info("Loading location tags from Supabase (bulk)...")
    location_tags_data = supabase.get_location_tags(limit=1_000_000)

    if not location_tags_data:
        logger.warning("No location tags found in Supabase")
        location_tags = pd.DataFrame(columns=["location_id", "tag_id", "score"])
    else:
        location_tags = pd.DataFrame(location_tags_data)
        # Add tag_text for easier lookup
        tag_id_to_text = tags_df.set_index("tag_id")["text"].to_dict()
        location_tags["tag_text"] = location_tags["tag_id"].map(tag_id_to_text)

    logger.info("Loaded %d location-tag associations", len(location_tags))

    # Load user tag affinities from Supabase
    logger.info("Loading user tag affinities from Supabase...")
    supabase_affinities = load_user_tag_affinities_from_supabase()

    if supabase_affinities.empty:
        logger.error("No user tag affinities found in Supabase")
        raise ValueError("No user tag affinities found in Supabase. Please ensure users have profiles.")

    logger.info("Loaded %d user tag affinities", len(supabase_affinities))
    user_tags = convert_supabase_affinities_to_profile_format(supabase_affinities, tags_df)

    # Build user history from affinity data
    user_history = (
        user_tags.groupby("user_id")
        .size()
        .reset_index(name="n_actions")
        .sort_values(by="n_actions", ascending=False)
    )
    logger.info("Found %d users", len(user_tags["user_id"].unique()))

    # Cache data
    DATA_CACHE["locations"] = locations
    DATA_CACHE["tags"] = tags_df
    DATA_CACHE["location_tags"] = location_tags
    DATA_CACHE["user_tags"] = user_tags
    DATA_CACHE["user_history"] = user_history
    DATA_CACHE["loaded"] = True

    logger.info("Data ready for API requests")


def get_photo_reference(place_id: str, api_key: str) -> Optional[str]:
    """Get the first photo resource name from Google Places API v1."""
    url = f"https://places.googleapis.com/v1/places/{place_id}"

    headers = {
        "Content-type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "id,displayName,photos",
    }

    try:
        res = requests.get(url, headers=headers, timeout=10).json()
        photos = res.get("photos", [])

        if not photos:
            return None

        # Extract photo resource name: "places/{place_id}/photos/{photo_id}"
        photo_name = photos[0].get("name")
        return photo_name
    except Exception as exc:
        logger.warning("Error fetching photo reference for %s: %s", place_id, exc)
        return None


def download_photo(photo_reference: str, api_key: str) -> Optional[bytes]:
    """Download image bytes from Google Places Photo API."""
    url = f"https://places.googleapis.com/v1/{photo_reference}/media?maxHeightPx=400&maxWidthPx=400&key={api_key}"

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.content
    except Exception as exc:
        logger.warning("Error downloading photo: %s", exc)
        return None


def classify_image_with_openai(image_bytes: bytes) -> Optional[int]:
    """
    Send image to OpenAI vision model for classification.
    Returns a quality score (1-3) or None on error.
    """
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        logger.warning("OPENAI_API_KEY not found, skipping photo classification")
        return None

    try:
        client = OpenAI(api_key=openai_key)
        base64_img = base64.b64encode(image_bytes).decode("utf-8")

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_img}"},
                        },
                        {
                            "type": "text",
                            "text": """You are a restaurant photo quality classifier. Evaluate this restaurant photo and assign a quality score:

- **3 (High Quality)**: Professional photography that represents the restaurant well
- **2 (Acceptable)**: Decent photo with some amateur qualities, but still usable
- **1 (Low Quality)**: Amateur smartphone photo that appears unprofessional

Evaluate based on:
1. Image clarity and resolution
2. Photography quality (professional vs smartphone)
3. Composition (angle, framing, completeness)
4. Lighting (natural, balanced, appropriate)
5. Staging and presentation

Be lenient - most professional photos should get a 3. Only penalize clearly amateur or poorly executed photos.

Provide your response as exactly:
Score: [1, 2, or 3]""",
                        },
                    ],
                }
            ],
            max_tokens=500,
        )

        result_text = response.choices[0].message.content
        score_match = re.search(r"Score:\\s*(\\d)", result_text)

        if score_match:
            score = int(score_match.group(1))
            logger.info("Photo classified with score: %d", score)
            return score
        logger.warning("Could not parse score from OpenAI response: %s", result_text)
        return None
    except Exception as exc:
        logger.error("Error classifying image with OpenAI: %s", exc, exc_info=True)
        return None


def classify_location_photo(google_place_id: str, api_key: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Fetch and classify the primary photo for a location.
    Returns (photo_reference, photo_score) tuple.
    """
    logger.info("Starting photo classification for place ID: %s", google_place_id)

    # Get photo reference
    photo_ref = get_photo_reference(google_place_id, api_key)
    if not photo_ref:
        logger.info("No photo found for location")
        return None, None

    logger.info("Found photo reference: %s", photo_ref)

    # Download photo
    image_bytes = download_photo(photo_ref, api_key)
    if not image_bytes:
        logger.warning("Failed to download photo")
        return photo_ref, None

    logger.info("Downloaded photo (%d bytes)", len(image_bytes))

    # Classify with OpenAI
    score = classify_image_with_openai(image_bytes)

    return photo_ref, score


def fetch_google_place_details(google_place_id: str) -> Optional[Dict[str, Any]]:
    """Fetch place details from Google Places API."""
    logger.info("Fetching details for Google Place ID: %s", google_place_id)

    api_key = GOOGLE_MAPS_API_KEY
    if not api_key:
        logger.error("GOOGLE_MAPS_API_KEY not found in environment variables")
        raise ValueError("GOOGLE_MAPS_API_KEY not found in environment variables")

    url = "https://maps.googleapis.com/maps/api/place/details/json"
    fields = ",".join(
        [
            "place_id",
            "name",
            "types",
            "rating",
            "price_level",
            "user_ratings_total",
            "editorial_summary",
            "opening_hours",
            "international_phone_number",
            "website",
            "review",
            "geometry",
            "vicinity",
            "business_status",
        ]
    )

    params = {
        "key": api_key,
        "place_id": google_place_id,
        "fields": fields,
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get("status") != "OK":
            logger.warning(
                "Google Places API returned status: %s for place ID: %s",
                data.get("status"),
                google_place_id,
            )
            return None

        logger.info(
            "Successfully fetched details for place: %s",
            data.get("result", {}).get("name", "Unknown"),
        )
        return data.get("result", {})
    except Exception as exc:
        logger.error(
            "Error fetching Google Place details for %s: %s",
            google_place_id,
            exc,
            exc_info=True,
        )
        return None


def process_and_tag_location(place_details: Dict[str, Any], supabase) -> Tuple[int, int]:
    """
    Process place details, create location entry, and generate tags.
    Returns (location_id, tags_count).
    """
    place_name = place_details.get("name", "Unknown")
    logger.info("Processing location: %s", place_name)

    # Extract data from place details
    geometry = place_details.get("geometry", {})
    location = geometry.get("location", {})
    opening_hours = place_details.get("opening_hours", {})
    reviews = place_details.get("reviews", [])

    logger.debug("Extracted %d reviews for %s", len(reviews), place_name)

    # Prepare location data
    location_data = {
        "name": place_details.get("name", ""),
        "google_place_id": place_details.get("place_id"),
        "vicinity": place_details.get("vicinity"),
        "lat": location.get("lat"),
        "lng": location.get("lng"),
        "rating": place_details.get("rating"),
        "user_ratings_total": place_details.get("user_ratings_total"),
        "price_level": place_details.get("price_level"),
        "business_status": place_details.get("business_status"),
        "editorial_summary": place_details.get("editorial_summary", {}).get("overview"),
        "website": place_details.get("website"),
        "international_phone_number": place_details.get("international_phone_number"),
        "types": ",".join(place_details.get("types", [])),
        "opening_hours_text": opening_hours.get("weekday_text", []),
        "opening_hours_periods": json.dumps(opening_hours.get("periods", []))
        if opening_hours.get("periods")
        else None,
        "open_now": opening_hours.get("open_now"),
    }

    # Create location in database
    logger.info("Creating location in database: %s", place_name)
    created_location = supabase.create_location(**location_data)
    location_id = created_location["location_id"]
    logger.info("Location created with ID: %s", location_id)

    # Process reviews for tagging
    reviews_data = []
    for review in reviews:
        reviews_data.append(
            {
                "location_id": location_id,
                "author_name": review.get("author_name", "anon"),
                "language": review.get("language", ""),
                "text": review.get("text", ""),
                "rating": review.get("rating"),
                "time": review.get("time"),
            }
        )

    # Create DataFrames for tagging
    # Parse types list from comma-separated string
    types_list = location_data["types"].split(",") if location_data["types"] else []
    types_list = [t.strip() for t in types_list if t.strip()]

    # Determine cuisine from types
    cuisine = place_details.get("types", ["unknown"])[0] if place_details.get("types") else "unknown"

    # Parse opening hours to determine schedule flags
    opening_hours_json = location_data.get("opening_hours_periods")
    is_open_late = False
    is_open_early = False
    is_sunday_open = False

    if opening_hours_json:
        try:
            periods = json.loads(opening_hours_json) if isinstance(opening_hours_json, str) else opening_hours_json
            for period in periods:
                open_time = period.get("open", {})
                close_time = period.get("close", {})
                open_day = open_time.get("day")
                close_day = close_time.get("day")
                open_hour = open_time.get("time", "0000")
                close_hour = close_time.get("time", "0000")

                # Convert to minutes
                if open_hour and len(str(open_hour)) >= 4:
                    open_minutes = int(str(open_hour)[:2]) * 60 + int(str(open_hour)[2:4])
                    if open_minutes < 8 * 60:  # Opens before 8am
                        is_open_early = True

                if close_hour and len(str(close_hour)) >= 4:
                    close_minutes = int(str(close_hour)[:2]) * 60 + int(str(close_hour)[2:4])
                    if close_minutes >= 23 * 60:  # Closes after 11pm
                        is_open_late = True

                if open_day == 0 or close_day == 0:  # Sunday
                    is_sunday_open = True
        except Exception as exc:
            logger.warning("Could not parse opening hours for location %s: %s", location_id, exc)

    # Determine price bucket
    price_level_val = location_data.get("price_level")
    if price_level_val is None or (isinstance(price_level_val, float) and pd.isna(price_level_val)):
        price_bucket = "unknown"
    elif price_level_val <= 1:
        price_bucket = "value"
    elif price_level_val == 2:
        price_bucket = "mid"
    elif price_level_val >= 3:
        price_bucket = "premium"
    else:
        price_bucket = "unknown"

    # Calculate derived scores (similar to load_locations)
    import numpy as np

    user_ratings = location_data.get("user_ratings_total") or 0
    log_reviews = np.log1p(user_ratings)
    rating_val = location_data.get("rating")
    rating_normalized = rating_val if rating_val is not None else 0.0

    location_df = pd.DataFrame(
        [
            {
                "location_id": location_id,
                "name": location_data["name"],
                "types": location_data["types"],
                "cuisine_primary": cuisine,
                "price_level": location_data["price_level"],
                "price_bucket": price_bucket,
                "rating": location_data["rating"],
                "user_ratings_total": location_data["user_ratings_total"],
                "opening_hours_periods": location_data["opening_hours_periods"],
                "is_open_late": is_open_late,
                "is_open_early": is_open_early,
                "is_sunday_open": is_sunday_open,
                "log_reviews": log_reviews,
                "popularity_score": 0.5,  # Default mid-range since we can't compare to others
                "expected_popularity": log_reviews,  # Use same as actual since no comparison
                "residual_popularity": 0.0,  # No residual without comparison
                "quality_score": rating_normalized / 5.0 if rating_normalized > 0 else 0.5,  # Normalize to 0-1
                "hidden_gem_score": 0.5,  # Default neutral score for new locations
            }
        ]
    )

    # Add types_list as a column (list type)
    location_df["types_list"] = [types_list]

    reviews_df = (
        pd.DataFrame(reviews_data)
        if reviews_data
        else pd.DataFrame(columns=["location_id", "author_name", "language", "text"])
    )

    # Build tags
    logger.info("Building tags for location %s", location_id)
    review_cfg = ReviewTagConfig(min_unique_authors=1, min_mentions=1)  # Lower thresholds for single location
    location_tags_df = build_location_tags(location_df, reviews_df, review_cfg)
    logger.info("Generated %d tags for location %s", len(location_tags_df), location_id)

    # Save tags to database
    tags_count = 0
    if not location_tags_df.empty:
        for _, tag_row in location_tags_df.iterrows():
            try:
                supabase.create_location_tag(
                    location_id=int(tag_row["location_id"]),
                    tag_id=str(tag_row["tag_id"]),
                    score=float(tag_row["score"]),
                    source=tag_row.get("source", "api"),
                    metadata=json.loads(tag_row["metadata"])
                    if isinstance(tag_row.get("metadata"), str)
                    else tag_row.get("metadata"),
                )
                tags_count += 1
            except Exception as exc:
                logger.warning(
                    "Could not save tag %s for location %s: %s",
                    tag_row.get("tag_text"),
                    location_id,
                    exc,
                )

    logger.info("Successfully saved %d tags for location %s", tags_count, location_id)
    return location_id, tags_count
