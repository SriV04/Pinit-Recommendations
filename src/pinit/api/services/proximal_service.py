from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from openai import OpenAI

from pinit.config.secrets import GOOGLE_PLACE_API_KEY
from pinit.config.settings import ReviewTagConfig
from pinit.core.recommendation.tag_taxonomy import get_tags_dataframe
from pinit.integrations.supabase import get_supabase_service

logger = logging.getLogger(__name__)

# Load emoji classification prompt from file
EMOJI_PROMPT_TEMPLATE: Optional[str] = None
EMOJI_PROMPT_FALLBACK = """Based on the restaurant information below, select a SINGLE emoji that best represents this location. Consider the cuisine type, atmosphere, and overall vibe.

Examples:
- Pizza place → 🍕
- Coffee shop → ☕
- Sushi restaurant → 🍣
- Burger joint → 🍔
- Fine dining → 🍽️
- Bar → 🍺

Respond with ONLY the emoji, nothing else.

Restaurant data:
{restaurant_json}"""

try:
    prompt_path = Path(__file__).parent / "prompt.md"
    if prompt_path.exists():
        EMOJI_PROMPT_TEMPLATE = prompt_path.read_text(encoding="utf-8")
        # Validate template has the placeholder
        if "{restaurant_json}" not in EMOJI_PROMPT_TEMPLATE:
            logger.warning("prompt.md missing {restaurant_json} placeholder, using fallback")
            EMOJI_PROMPT_TEMPLATE = None
        else:
            logger.info("Successfully loaded emoji classification prompt from %s", prompt_path)
    else:
        logger.warning("Emoji prompt file not found at %s, using fallback", prompt_path)
except Exception as exc:
    logger.error("Error loading emoji prompt file: %s, using fallback", exc)
    EMOJI_PROMPT_TEMPLATE = None



# --------------------------------------------------- PHOTO CLASSSIFICATION HELPER FUNCTIONS ---------------------------------------------------

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


def download_photo(photo_reference: str, api_key: str) -> Optional[Tuple[bytes, str]]:
    """Download image bytes and content type from Google Places Photo API."""
    url = f"https://places.googleapis.com/v1/{photo_reference}/media?maxHeightPx=400&maxWidthPx=400&key={api_key}"

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "image/jpeg")
        return response.content, content_type
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
        logger.info("OpenAI response: %s", result_text)
        score_match = re.search(r"Score:\s*(\d)", result_text)

        if score_match:
            score = int(score_match.group(1))
            logger.info("Photo classified with score: %d", score)
            return score
        logger.warning("Could not parse score from OpenAI response: %s", result_text)
        return None
    except Exception as exc:
        logger.error("Error classifying image with OpenAI: %s", exc, exc_info=True)
        return None
    

def classify_location_photo(
    google_place_id: str, api_key: str
) -> Tuple[Optional[str], Optional[int], Optional[bytes], Optional[str]]:
    """
    Fetch and classify the primary photo for a location.
    Returns (photo_reference, photo_score, image_bytes, content_type) tuple.
    """
    logger.info("Starting photo classification for place ID: %s", google_place_id)

    # Get photo reference
    photo_ref = get_photo_reference(google_place_id, api_key)
    if not photo_ref:
        logger.info("No photo found for location")
        return None, None, None, None

    logger.info("Found photo reference: %s", photo_ref)

    # Download photo
    download_result = download_photo(photo_ref, api_key)
    if not download_result:
        logger.warning("Failed to download photo")
        return photo_ref, None, None, None

    image_bytes, content_type = download_result
    logger.info("Downloaded photo (%d bytes)", len(image_bytes))

    # Classify with OpenAI
    score = classify_image_with_openai(image_bytes)
    
    if score is None:
        logger.warning("Photo classification returned None, but photo was downloaded successfully")

    return photo_ref, score, image_bytes, content_type


# --------------------------------------------------- LOCATION EMOJI HELPER FUNCTION ---------------------------------------------------

def add_location_emoji(place_details: Dict[str, Any]) -> Optional[str]:
    """Generate an emoji for a location using OpenAI's sophisticated 6-step classification process.

    Uses a comprehensive prompt that prioritizes:
    1. Drink establishments (coffee, tea, bars)
    2. Signature food types (pizza, sushi, ramen, etc.)
    3. Cuisine by country/origin (Italian, Japanese, etc.)
    4. Venue/place type (fine dining, rooftop, etc.)
    5. Dietary focus (vegan, etc.)
    6. Generic fallback

    Args:
        place_details: Google Places data containing name, types, price_level, rating,
                      editorial_summary, reviews, vicinity

    Returns:
        Single emoji string (e.g., "🍕") or None if generation fails
    """
    # 1. Check API key
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        logger.warning("OPENAI_API_KEY not found, skipping emoji generation")
        return None

    # 2. Extract relevant context
    name = place_details.get("name", "Unknown")
    types = place_details.get("types", [])
    # Handle types as either list or comma-separated string
    if isinstance(types, str):
        types = [t.strip() for t in types.split(",") if t.strip()]

    price_level = place_details.get("price_level")
    rating = place_details.get("rating")

    # Handle editorial_summary as both dict and string
    editorial_summary = place_details.get("editorial_summary", {})
    if isinstance(editorial_summary, dict):
        editorial_summary_text = editorial_summary.get("overview", "")
    else:
        editorial_summary_text = editorial_summary or ""

    # Extract vicinity
    vicinity = place_details.get("vicinity", "")

    # Extract and limit reviews to top 3
    reviews = place_details.get("reviews", [])
    if reviews and isinstance(reviews, list):
        reviews = reviews[:3]

    # 3. Build restaurant JSON matching prompt.md expectations
    restaurant_data = {
        "name": name,
        "types": types,
        "rating": rating,
        "price_level": price_level,
        "editorial_summary": {"overview": editorial_summary_text} if editorial_summary_text else None,
        "reviews": [
            {
                "text": review.get("text", ""),
                "rating": review.get("rating"),
                "author": review.get("author_name", ""),
                "time": review.get("time")
            }
            for review in reviews
        ] if reviews else [],
        "vicinity": vicinity
    }

    # 4. Build prompt with template replacement
    if EMOJI_PROMPT_TEMPLATE:
        restaurant_json = json.dumps(restaurant_data, indent=2, ensure_ascii=False)
        prompt_content = EMOJI_PROMPT_TEMPLATE.replace("{restaurant_json}", restaurant_json)
    else:
        # Use fallback prompt
        restaurant_json = json.dumps(restaurant_data, indent=2, ensure_ascii=False)
        prompt_content = EMOJI_PROMPT_FALLBACK.replace("{restaurant_json}", restaurant_json)

    # 5. Call OpenAI
    try:
        client = OpenAI(api_key=openai_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": prompt_content
                }
            ],
            max_tokens=20,
            temperature=0.1,
        )

        result_text = response.choices[0].message.content.strip()
        logger.info("Generated emoji for %s: %s", name, result_text)

        # 5. Validate (emoji should be 1-4 chars)
        if result_text and len(result_text) <= 4:
            return result_text

        logger.warning("Invalid emoji response: %s", result_text)
        return None

    except Exception as exc:
        logger.error("Error generating emoji for %s: %s", name, exc, exc_info=True)
        return None
    

# --------------------------------------------------- GOOGLE PLACES DETAILS FUNCTION ---------------------------------------------------

def fetch_google_place_details(google_place_id: str) -> Optional[Dict[str, Any]]:
    """Fetch place details from Google Places API v1 (New) with enterprise-tier fields.

    Uses https://places.googleapis.com/v1/places/PLACE_ID with the same
    enterprise-tier field mask as the stage_a discovery pipeline, capturing
    vibe booleans, serves booleans, reviews, photos, and review summaries.

    Returns a dict in the legacy Place Details format so that existing consumers
    (process_and_tag_location, add_location_emoji) continue to work, with
    additional enterprise-tier fields included as extra keys.
    """

        # Price level mapping — v1 API returns enum strings, legacy format uses numeric
    PRICE_LEVEL_MAP: Dict[str, int] = {
        "PRICE_LEVEL_FREE": 0,
        "PRICE_LEVEL_INEXPENSIVE": 1,
        "PRICE_LEVEL_MODERATE": 2,
        "PRICE_LEVEL_EXPENSIVE": 3,
        "PRICE_LEVEL_VERY_EXPENSIVE": 4,
    }

    # Place Details (New) v1 field mask — Enterprise tier
    # No "places." prefix for single-place lookups
    PLACE_DETAILS_FIELD_MASK = ",".join([
        # --- Essentials ---
        "id",
        "displayName",
        "types",
        "formattedAddress",
        "shortFormattedAddress",
        "location",
        "businessStatus",
        "googleMapsUri",
        "photos",
        # --- Pro-tier ---
        "rating",
        "userRatingCount",
        "priceLevel",
        "currentOpeningHours",
        # --- Enterprise-tier ---
        "reviews",
        "websiteUri",
        "internationalPhoneNumber",
        "editorialSummary",
        "reviewSummary",
        "goodForChildren",
        "goodForGroups",
        "goodForWatchingSports",
        "liveMusic",
        "outdoorSeating",
        "servesBeer",
        "servesBreakfast",
        "servesBrunch",
        "servesCocktails",
        "servesCoffee",
        "servesDessert",
        "servesDinner",
        "servesLunch",
        "servesVegetarianFood",
        "servesWine",
    ])
    logger.info("Fetching details for Google Place ID: %s", google_place_id)

    api_key = GOOGLE_PLACE_API_KEY
    if not api_key:
        logger.error("GOOGLE_PLACE_API_KEY not found in environment variables")
        raise ValueError("GOOGLE_PLACE_API_KEY not found in environment variables")

    url = f"https://places.googleapis.com/v1/places/{google_place_id}"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": PLACE_DETAILS_FIELD_MASK,
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        place = response.json()

        if not place.get("id"):
            logger.warning(
                "Google Places API v1 returned no id for place ID: %s",
                google_place_id,
            )
            return None

        # --- Translate v1 response to legacy-compatible format ---
        display_name = place.get("displayName", {})
        name = (display_name.get("text") or "").strip()

        location = place.get("location", {})

        types_list = place.get("types", [])

        # Price level: v1 returns enum string, convert to numeric
        raw_price = place.get("priceLevel")
        price_level = PRICE_LEVEL_MAP.get(raw_price) if isinstance(raw_price, str) else raw_price

        # Opening hours
        current_hours = place.get("currentOpeningHours", {})

        # Reviews: translate v1 format to legacy format
        raw_reviews = place.get("reviews", [])
        legacy_reviews = []
        if raw_reviews:
            for r in raw_reviews:
                legacy_reviews.append({
                    "author_name": r.get("authorAttribution", {}).get("displayName", ""),
                    "text": r.get("text", {}).get("text", ""),
                    "rating": r.get("rating"),
                    "language": r.get("text", {}).get("languageCode", ""),
                    "time": r.get("publishTime"),
                    "relative_time_description": r.get("relativePublishTimeDescription", ""),
                })

        # Editorial summary
        editorial_summary_obj = place.get("editorialSummary", {})
        editorial_summary_text = editorial_summary_obj.get("text", "") if isinstance(editorial_summary_obj, dict) else ""

        # Review summary
        review_summary_obj = place.get("reviewSummary", {})
        review_summary_text = review_summary_obj.get("text", {}).get("text") if isinstance(review_summary_obj, dict) else None

        # Photos: extract resource names
        raw_photos = place.get("photos", [])
        photos_json = None
        if raw_photos:
            photos_json = [
                {"name": p.get("name"), "widthPx": p.get("widthPx"), "heightPx": p.get("heightPx")}
                for p in raw_photos if p.get("name")
            ]

        # Build result dict — legacy-compatible keys for existing consumers,
        # plus enterprise-tier fields matching stage_a's _build_location_row
        result = {
            # --- Legacy-compatible keys (used by process_and_tag_location / add_location_emoji) ---
            "place_id": place.get("id"),
            "name": name,
            "types": types_list,
            "geometry": {"location": {"lat": location.get("latitude"), "lng": location.get("longitude")}},
            "vicinity": place.get("shortFormattedAddress"),
            "formatted_address": place.get("formattedAddress"),
            "rating": place.get("rating"),
            "user_ratings_total": place.get("userRatingCount"),
            "price_level": price_level,
            "business_status": place.get("businessStatus"),
            "editorial_summary": {"overview": editorial_summary_text} if editorial_summary_text else {},
            "website": place.get("websiteUri"),
            "international_phone_number": place.get("internationalPhoneNumber"),
            "opening_hours": {
                "weekday_text": current_hours.get("weekdayDescriptions", []),
                "periods": current_hours.get("periods", []),
                "open_now": current_hours.get("openNow"),
            },
            "reviews": legacy_reviews,
            "google_maps_uri": place.get("googleMapsUri"),
            # --- Photos (resource names for later Photo API fetches) ---
            "photos": photos_json,
            # --- Review summary ---
            "review_summary": review_summary_text,
            # --- Enterprise-tier: vibe / atmosphere booleans ---
            "good_for_children": place.get("goodForChildren"),
            "good_for_groups": place.get("goodForGroups"),
            "good_for_watching_sports": place.get("goodForWatchingSports"),
            "live_music": place.get("liveMusic"),
            "outdoor_seating": place.get("outdoorSeating"),
            # --- Enterprise-tier: serves booleans ---
            "serves_beer": place.get("servesBeer"),
            "serves_breakfast": place.get("servesBreakfast"),
            "serves_brunch": place.get("servesBrunch"),
            "serves_cocktails": place.get("servesCocktails"),
            "serves_coffee": place.get("servesCoffee"),
            "serves_dessert": place.get("servesDessert"),
            "serves_dinner": place.get("servesDinner"),
            "serves_lunch": place.get("servesLunch"),
            "serves_vegetarian_food": place.get("servesVegetarianFood"),
            "serves_wine": place.get("servesWine"),
        }

        logger.info(
            "Successfully fetched details for place: %s (v1 API, enterprise tier)",
            name or "Unknown",
        )

        # --- Insert into Supabase ---
        opening_hours = result["opening_hours"]
        location_data = {
            "google_place_id": result["place_id"],
            "name": name,
            "vicinity": result["vicinity"],
            "lat": location.get("latitude"),
            "lng": location.get("longitude"),
            "types": ",".join(t.lower() for t in types_list) if types_list else None,
            "business_status": result["business_status"],
            "rating": result["rating"],
            "user_ratings_total": result["user_ratings_total"],
            "price_level": price_level,
            "opening_hours_text": opening_hours.get("weekday_text", []),
            "opening_hours_periods": json.dumps(opening_hours.get("periods", []))
            if opening_hours.get("periods")
            else None,
            "open_now": opening_hours.get("open_now"),
            "editorial_summary": editorial_summary_text or None,
            "website": result["website"],
            "international_phone_number": result["international_phone_number"],
            "google_maps_uri": result["google_maps_uri"],
            "photos": photos_json,
            "reviews": json.dumps(legacy_reviews) if legacy_reviews else None,
            "review_summary": review_summary_text,
            "good_for_children": result["good_for_children"],
            "good_for_groups": result["good_for_groups"],
            "good_for_watching_sports": result["good_for_watching_sports"],
            "live_music": result["live_music"],
            "outdoor_seating": result["outdoor_seating"],
            "serves_beer": result["serves_beer"],
            "serves_breakfast": result["serves_breakfast"],
            "serves_brunch": result["serves_brunch"],
            "serves_cocktails": result["serves_cocktails"],
            "serves_coffee": result["serves_coffee"],
            "serves_dessert": result["serves_dessert"],
            "serves_dinner": result["serves_dinner"],
            "serves_lunch": result["serves_lunch"],
            "serves_vegetarian_food": result["serves_vegetarian_food"],
            "serves_wine": result["serves_wine"],
        }

        supabase = get_supabase_service()
        created = supabase.create_location(**location_data)
        result["location_id"] = created["location_id"]
        logger.info("Created location in DB with ID: %s for place: %s", created["location_id"], name)

        return result

    except Exception as exc:
        logger.error(
            "Error fetching Google Place details for %s: %s",
            google_place_id,
            exc,
            exc_info=True,
        )
        return None


def search_google_places(
    prompt: str,
    latitude: float,
    longitude: float,
    radius_km: float,
    max_results: int,
) -> List[str]:
    """Search Google Places with a text prompt and return place IDs."""
    api_key = GOOGLE_PLACE_API_KEY
    if not api_key:
        logger.error("GOOGLE_PLACE_API_KEY not found in environment variables")
        raise ValueError("GOOGLE_PLACE_API_KEY not found in environment variables")

    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {
        "query": prompt,
        "location": f"{latitude},{longitude}",
        "radius": int(radius_km * 1000),
        "key": api_key,
    }

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()

    if data.get("status") not in {"OK", "ZERO_RESULTS"}:
        raise ValueError(f"Google Places Text Search error: {data.get('status')}")

    results = data.get("results", [])
    place_ids = [item.get("place_id") for item in results if item.get("place_id")]
    return place_ids[:max_results]

def text_search(
    query: str,
    place_types: List[str],
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    radius_m: Optional[float] = None,
) -> Tuple[List[dict], int]:
    """Text Search (New) – Places API v1 wrapper.

    Makes one POST to the Text Search (New) endpoint per entry in
    *place_types* (the API only accepts a single ``includedType`` per
    request), deduplicates the combined results by place ID, and returns
    the aggregated list together with the total number of API calls consumed.

    Returns the same enterprise-tier fields as Nearby Search: rating,
    userRatingCount, priceLevel, currentOpeningHours, reviews, websiteUri,
    photos, reviewSummary, vibe booleans, and serves booleans.

    Parameters
    ----------
    query:
        Free-text search query (``textQuery`` field).
    place_types:
        List of Google Places type strings.  One API call is made for each
        type.  If empty, a single call is made without an ``includedType``
        constraint.
    lat, lng, radius_m:
        Optional centre + radius for a ``locationBias`` circle.  When all
        three are supplied the search is biased (not restricted) toward that
        area.
    """
    # --- Inline constants ---
    _URL = "https://places.googleapis.com/v1/places:searchText"
    _FIELD_MASK = ",".join([
        "places.id",
        "places.displayName",
        "places.types",
        "places.formattedAddress",
        "places.shortFormattedAddress",
        "places.location",
        "places.businessStatus",
        "places.googleMapsUri",
        "places.photos",
        # Pro-tier
        "places.rating",
        "places.userRatingCount",
        "places.priceLevel",
        "places.currentOpeningHours",
        # Enterprise-tier
        "places.reviews",
        "places.websiteUri",
        "places.reviewSummary",
        "places.goodForChildren",
        "places.goodForGroups",
        "places.goodForWatchingSports",
        "places.liveMusic",
        "places.outdoorSeating",
        "places.servesBeer",
        "places.servesBreakfast",
        "places.servesBrunch",
        "places.servesCocktails",
        "places.servesCoffee",
        "places.servesDessert",
        "places.servesDinner",
        "places.servesLunch",
        "places.servesVegetarianFood",
        "places.servesWine",
    ])
    _MAX_RESULTS = 20
    _MAX_RETRIES = 6
    _BACKOFF_BASE = 2.0
    _MAX_WAIT = 30.0
    _SLEEP_S = 0.15

    api_key = GOOGLE_PLACE_API_KEY
    if not api_key:
        raise ValueError("GOOGLE_PLACE_API_KEY not set")

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": _FIELD_MASK,
    }

    def _jitter_sleep(seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds * random.uniform(0.85, 1.15))

    # Build the base body shared by every call in this invocation.
    base_body: Dict[str, Any] = {
        "textQuery": query,
        "maxResultCount": _MAX_RESULTS,
    }
    if lat is not None and lng is not None and radius_m is not None:
        base_body["locationBias"] = {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(radius_m),
            }
        }

    # Text Search only supports a single includedType; iterate over types.
    types_to_query: List[Optional[str]] = list(place_types) if place_types else [None]

    seen_ids: dict = {}
    total_calls = 0

    for place_type in types_to_query:
        body = dict(base_body)
        if place_type is not None:
            body["includedType"] = place_type

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = requests.post(_URL, json=body, headers=headers, timeout=30)
            except requests.RequestException as exc:
                wait = min(_SLEEP_S * _BACKOFF_BASE ** (attempt - 1), _MAX_WAIT)
                logger.warning("  ⚠ HTTP error on attempt %d/%d (type=%s): %s — retrying in %.1fs",
                               attempt, _MAX_RETRIES, place_type, exc, wait)
                _jitter_sleep(wait)
                continue

            if resp.status_code == 429:
                wait = min(5.0 * _BACKOFF_BASE ** (attempt - 1), _MAX_WAIT)
                logger.warning("  ⚠ Rate limited (429) on attempt %d/%d (type=%s) — backing off %.1fs",
                               attempt, _MAX_RETRIES, place_type, wait)
                _jitter_sleep(wait)
                continue

            if resp.status_code == 403:
                msg = resp.text[:200]
                raise RuntimeError(f"Google Places API 403 Forbidden: {msg}")

            if resp.status_code >= 500:
                wait = min(2.0 * _BACKOFF_BASE ** (attempt - 1), _MAX_WAIT)
                logger.warning("  ⚠ Server error %d on attempt %d/%d (type=%s) — retrying in %.1fs",
                               resp.status_code, attempt, _MAX_RETRIES, place_type, wait)
                _jitter_sleep(wait)
                continue

            if resp.status_code == 400:
                logger.error("  ✗ Bad request (400) for type=%s: %s", place_type, resp.text[:300])
                total_calls += 1
                break  # skip this type, continue to next

            try:
                data = resp.json()
            except ValueError:
                logger.warning("  ⚠ Invalid JSON on attempt %d/%d (type=%s)", attempt, _MAX_RETRIES, place_type)
                _jitter_sleep(min(_SLEEP_S * _BACKOFF_BASE ** (attempt - 1), _MAX_WAIT))
                continue

            total_calls += 1
            for place in data.get("places", []):
                pid = place.get("id")
                if pid and pid not in seen_ids:
                    seen_ids[pid] = place
            break  # successful response — move to next type
        else:
            raise RuntimeError(
                f"text_search failed after {_MAX_RETRIES} retries for type={place_type!r}"
            )

        _jitter_sleep(_SLEEP_S)

    return list(seen_ids.values()), total_calls