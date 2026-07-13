"""Fill missing location story fields from canonical Google evidence.

This is the fallback used when menu crawling cannot provide the product fields
needed by the app. It deliberately does not invent a menu or recommended dishes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Annotated

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator

from pinit.config.secrets import XAI_API_KEY
from pinit.integrations.supabase import get_supabase_service

logger = logging.getLogger(__name__)

XAI_BASE_URL = "https://api.x.ai/v1"
MODEL = "grok-4-fast-non-reasoning"

DietaryScore = Annotated[int, Field(ge=0, le=100)]
Analyzer = Callable[[dict[str, Any]], Awaitable["LocationContentFallback | dict[str, Any]"]]


class LocationContentFallback(BaseModel):
    """Validated content generated only from the supplied location evidence."""

    summary: str
    cuisine_primary: str
    dietary_requirement_vector: list[DietaryScore] = Field(min_length=6, max_length=6)

    @field_validator("summary", "cuisine_primary")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_dietary_vector(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 6


def _canonical_google_evidence(row: dict[str, Any]) -> dict[str, Any]:
    """Return only useful, bounded evidence already stored on the location."""
    evidence_fields = (
        "name",
        "vicinity",
        "types",
        "price_level",
        "rating",
        "user_ratings_total",
        "reviews",
        "review_summary",
        "editorial_summary",
        "generative_summary",
        "serves_breakfast",
        "serves_brunch",
        "serves_lunch",
        "serves_dinner",
        "serves_beer",
        "serves_wine",
        "serves_cocktails",
        "serves_vegetarian_food",
        "takeout",
        "delivery",
        "dine_in",
        "outdoor_seating",
        "live_music",
        "good_for_children",
        "good_for_groups",
        "good_for_watching_sports",
        "allows_dogs",
        "opening_hours",
    )
    evidence = {field: row[field] for field in evidence_fields if row.get(field) not in (None, "", [], {})}

    # Reviews are the largest field. Preserve their useful evidence while
    # bounding each item and the total sent to the model.
    reviews = evidence.get("reviews")
    if isinstance(reviews, list):
        evidence["reviews"] = [
            {
                key: review[key]
                for key in ("rating", "text", "relativePublishTimeDescription", "authorAttribution")
                if isinstance(review, dict) and review.get(key) not in (None, "", [], {})
            }
            for review in reviews[:5]
            if isinstance(review, dict)
        ]
    return evidence


async def _analyze_location_content(row: dict[str, Any]) -> LocationContentFallback:
    if not XAI_API_KEY:
        raise RuntimeError("XAI_API_KEY is required for location content fallback")

    evidence = _canonical_google_evidence(row)
    client = AsyncOpenAI(api_key=XAI_API_KEY, base_url=XAI_BASE_URL)
    response = await client.chat.completions.create(
        model=MODEL,
        max_tokens=700,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "You write concise restaurant and place metadata using only the supplied evidence. "
                    "Return valid JSON. Never claim facts that are not supported by the evidence."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Create a short, useful summary, one lowercase primary cuisine/category, and six "
                    "dietary suitability scores from 0 to 100. The score order must be exactly: halal, "
                    "vegan, gluten-free, vegetarian, dairy-free, nut-free. If evidence is weak, use "
                    "conservative scores rather than fabricating certainty. Return exactly these JSON keys: "
                    "summary, cuisine_primary, dietary_requirement_vector.\n\nEvidence:\n"
                    + json.dumps(evidence, ensure_ascii=False, default=str)
                ),
            },
        ],
    )
    raw = response.choices[0].message.content
    if not raw:
        raise RuntimeError("Location content fallback returned an empty response")
    return LocationContentFallback.model_validate_json(raw)


async def fill_missing_location_content(
    row: dict[str, Any],
    *,
    analyzer: Analyzer | None = None,
) -> dict[str, Any]:
    """Fill only missing story fields and persist one non-destructive patch."""
    missing_summary = not _has_text(row.get("generated_summary"))
    missing_cuisine = not _has_text(row.get("cuisine_primary"))
    missing_dietary = not _has_dietary_vector(row.get("dietary_requirement_vector"))
    if not any((missing_summary, missing_cuisine, missing_dietary)):
        return {}

    analyzed = await (analyzer or _analyze_location_content)(row)
    result = (
        analyzed
        if isinstance(analyzed, LocationContentFallback)
        else LocationContentFallback.model_validate(analyzed)
    )

    update: dict[str, Any] = {"menu_analysis_confidence": "google_fallback"}
    if missing_summary:
        update["generated_summary"] = result.summary
    if missing_cuisine:
        update["cuisine_primary"] = result.cuisine_primary
    if missing_dietary:
        update["dietary_requirement_vector"] = result.dietary_requirement_vector

    location_id = row.get("location_id")
    if location_id is None:
        raise ValueError("location_id is required to persist fallback content")
    saved = get_supabase_service().update_location(int(location_id), **update)
    if saved is None:
        raise RuntimeError(f"Failed to persist fallback content for location {location_id}")
    logger.info("Filled missing Google fallback content for location %s: %s", location_id, sorted(update))
    return update
