from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

from pinit.api.schemas_magic import MagicIntent


DATE_TERMS = {"date", "romantic", "first date", "date night"}
DINNER_TERMS = {"dinner", "supper", "evening"}
BRUNCH_TERMS = {"brunch"}
COFFEE_TERMS = {"coffee", "cafe", "work cafe", "laptop"}
DRINKS_TERMS = {"drinks", "cocktails", "bar", "wine"}
VIRAL_TERMS = {"viral", "tiktok", "instagram", "trending"}
NEW_OPENING_TERMS = {"new opening", "just opened", "newly opened"}
RECENT_TERMS = {"hot", "new", "recent", "latest", "opening", "buzz"}


def _normalise_prompt(raw_prompt: str) -> str:
    return re.sub(r"\s+", " ", raw_prompt.lower()).strip()


def geo_bucket(lat: float, lng: float) -> str:
    # 2 decimals ≈ a ~1km cell. The AI enrichment describes "what's hot in this
    # area", so a neighbourhood-scale bucket (rather than a ~110m one) keeps
    # nearby searches on the same cache key and lifts the hit rate sharply.
    return f"{round(lat, 2):.2f}:{round(lng, 2):.2f}"


def radius_bucket(radius_km: float) -> str:
    if radius_km <= 1.0:
        return "1km"
    if radius_km <= 2.5:
        return "2km"
    if radius_km <= 5.0:
        return "5km"
    if radius_km <= 10.0:
        return "10km"
    return "50km"


def _contains_any(text: str, terms: set[str]) -> bool:
    return any(term in text for term in terms)


def _primary_intent(intent: MagicIntent, prompt_text: str) -> str:
    if intent.occasion == "date_night" or _contains_any(prompt_text, DATE_TERMS):
        return "date_spot"
    if intent.occasion == "group":
        return "group_dinner"
    if _contains_any(prompt_text, BRUNCH_TERMS):
        return "brunch"
    if _contains_any(prompt_text, COFFEE_TERMS):
        return "work_cafe" if "work" in prompt_text or "laptop" in prompt_text else "coffee"
    if _contains_any(prompt_text, DRINKS_TERMS):
        return "drinks"
    if "hidden" in prompt_text:
        return "hidden_gem"
    if "cheap" in prompt_text:
        return "cheap_eats"
    if _contains_any(prompt_text, VIRAL_TERMS):
        return "viral_food"
    if _contains_any(prompt_text, NEW_OPENING_TERMS):
        return "new_opening"
    return "food_discovery"


def _meal(intent: MagicIntent, prompt_text: str) -> Optional[str]:
    if _contains_any(prompt_text, BRUNCH_TERMS):
        return "brunch"
    if _contains_any(prompt_text, COFFEE_TERMS):
        return "coffee"
    if _contains_any(prompt_text, DRINKS_TERMS):
        return "drinks"
    if _contains_any(prompt_text, DINNER_TERMS) or intent.occasion in {"date_night", "group"}:
        return "dinner"
    if "lunch" in prompt_text:
        return "lunch"
    if "breakfast" in prompt_text:
        return "breakfast"
    if "dessert" in prompt_text:
        return "dessert"
    return None


def _freshness(prompt_text: str) -> str:
    if _contains_any(prompt_text, VIRAL_TERMS):
        return "viral"
    if _contains_any(prompt_text, NEW_OPENING_TERMS):
        return "new_opening"
    if _contains_any(prompt_text, RECENT_TERMS):
        return "recent"
    return "evergreen"


def _budget(intent: MagicIntent) -> Optional[str]:
    if intent.budget == "cheap":
        return "low"
    if intent.budget == "moderate":
        return "mid"
    if intent.budget == "expensive":
        return "high"
    return None


def _venue_types(intent: MagicIntent, prompt_text: str) -> List[str]:
    if _contains_any(prompt_text, COFFEE_TERMS):
        return ["cafe"]
    if _contains_any(prompt_text, DRINKS_TERMS):
        return ["bar"]
    return list(intent.included_types or ["restaurant"])


def _canonical_vibes(primary_intent: str, intent: MagicIntent) -> List[str]:
    vibes = set(intent.vibe_tags)
    if primary_intent == "date_spot":
        vibes -= {"cute", "romantic", "cosy"}
    return sorted(vibes)


def build_magic_ai_signature(
    *,
    intent: MagicIntent,
    raw_prompt: str,
    lat: float,
    lng: float,
    radius_km: float,
) -> Dict[str, Any]:
    prompt_text = _normalise_prompt(raw_prompt)
    primary_intent = _primary_intent(intent, prompt_text)
    canonical = {
        "primary_intent": primary_intent,
        "meal": _meal(intent, prompt_text),
        "cuisines": sorted(set(intent.cuisines)),
        "vibes": _canonical_vibes(primary_intent, intent),
        "venue_types": sorted(set(_venue_types(intent, prompt_text))),
        "budget": _budget(intent),
        "freshness": _freshness(prompt_text),
    }
    canonical_text = " ".join(
        str(part)
        for part in [
            canonical["primary_intent"],
            canonical["meal"] or "any",
            " ".join(canonical["cuisines"]),
            " ".join(canonical["vibes"]),
            " ".join(canonical["venue_types"]),
            canonical["budget"] or "any",
            canonical["freshness"],
        ]
        if part
    )
    # Hash only the dimensions that materially change which venues an area
    # search should surface. Dropping vibes/venue_types (noisy, free-text
    # derived, and largely implied by primary_intent) collapses many prompt
    # variations onto the same cache key without hurting relevance. freshness
    # is already a separate component of the cache key.
    hash_payload = json.dumps(
        {
            "primary_intent": canonical["primary_intent"],
            "meal": canonical["meal"],
            "cuisines": canonical["cuisines"],
            "budget": canonical["budget"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        **canonical,
        "geo_bucket": geo_bucket(lat, lng),
        "radius_bucket": radius_bucket(radius_km),
        "canonical_text": canonical_text,
        "intent_hash": hashlib.sha1(hash_payload.encode("utf-8")).hexdigest()[:16],
    }
