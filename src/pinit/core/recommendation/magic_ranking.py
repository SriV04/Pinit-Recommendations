from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

from pinit.api.schemas_magic import MagicIntent
from pinit.core.recommendation.magic_explanations import build_match_reasons


_VIBE_KEYWORDS = {
    "casual": {"casual", "relaxed", "neighbourhood", "neighborhood", "pub"},
    "cosy": {"cosy", "cozy", "intimate", "warm"},
    "cute": {"cute", "pretty", "charming"},
    "fine_dining": {"fine", "formal", "elegant", "tasting", "michelin"},
    "formal": {"formal", "fine", "elegant", "upscale"},
    "good_for_groups": {"group", "groups", "sharing", "large", "casual"},
    "hidden_gem": {"hidden", "local", "neighbourhood", "neighborhood"},
    "lively": {"lively", "busy", "music", "bar"},
    "quiet": {"quiet", "calm", "intimate"},
    "romantic": {"romantic", "date", "intimate", "cosy", "cozy"},
    "stylish": {"stylish", "design", "modern", "upscale"},
    "trendy": {"trendy", "modern", "stylish", "vibey"},
    "upscale": {"upscale", "fine", "formal", "elegant", "bougie"},
}

_OCCASION_KEYWORDS = {
    "birthday": {"birthday", "group", "groups", "cocktail", "lively"},
    "brunch": {"brunch", "breakfast", "cafe"},
    "date_night": {"date", "romantic", "cosy", "cozy", "intimate", "wine"},
    "group": {"group", "groups", "sharing", "large", "casual"},
    "work": {"formal", "quiet", "business", "elegant"},
}

_TEXT_FIELDS = (
    "name",
    "vicinity",
    "formatted_address",
    "cuisine_primary",
    "types",
    "editorial_summary",
    "review_summary",
)


def _normalise_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _coordinate_cell(candidate: Mapping[str, Any]) -> Optional[tuple[int, int]]:
    lat = candidate.get("lat")
    lng = candidate.get("lng")
    if lat is None or lng is None:
        return None
    return (round(_float(lat) * 1000), round(_float(lng) * 1000))


def dedupe_magic_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate Magic candidates by Google ID, location ID, then name/proximity."""
    seen_google_ids: set[str] = set()
    seen_location_ids: set[int] = set()
    seen_name_cells: set[tuple[str, int, int]] = set()
    deduped: List[Dict[str, Any]] = []

    for candidate in candidates:
        google_place_id = str(candidate.get("google_place_id") or "").strip()
        if google_place_id:
            if google_place_id in seen_google_ids:
                continue
            seen_google_ids.add(google_place_id)

        location_id = candidate.get("location_id")
        if location_id is not None:
            location_id_int = _int(location_id)
            if location_id_int in seen_location_ids:
                continue
            seen_location_ids.add(location_id_int)

        name = _normalise_name(candidate.get("name"))
        cell = _coordinate_cell(candidate)
        if name and cell is not None:
            name_cell = (name, cell[0], cell[1])
            if name_cell in seen_name_cells:
                continue
            seen_name_cells.add(name_cell)

        deduped.append(candidate)

    return deduped


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, float) and math.isnan(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes"}:
            return True
        if text in {"false", "0", "no"}:
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    return None


def _normalised_text(candidate: Mapping[str, Any]) -> str:
    parts: List[str] = []
    for field in _TEXT_FIELDS:
        value = candidate.get(field)
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            parts.extend(str(item) for item in value)
        else:
            parts.append(str(value))
    return re.sub(r"\s+", " ", " ".join(parts).lower()).strip()


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    return any(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) for term in terms)


def _google_quality_score(candidate: Mapping[str, Any]) -> float:
    explicit = candidate.get("quality_score")
    if explicit is not None:
        return _clamp(_float(explicit))

    rating_score = _clamp(_float(candidate.get("rating")) / 5.0)
    review_count = max(0.0, _float(candidate.get("user_ratings_total")))
    review_score = min(math.log1p(review_count) / math.log1p(1000.0), 1.0)
    if rating_score <= 0.0 and review_score <= 0.0:
        return 0.35
    return _clamp((rating_score * 0.75) + (review_score * 0.25))


def score_budget_match(candidate: Mapping[str, Any], intent: MagicIntent) -> float:
    if not intent.budget:
        return 0.5

    price = candidate.get("price_level")
    if price is None:
        return 0.5
    price_int = _int(price, default=-1)
    if price_int < 0:
        return 0.5

    if intent.budget == "cheap":
        if price_int <= 2:
            return 1.0
        if price_int == 3:
            return 0.25
        return 0.0

    if intent.budget == "moderate":
        if price_int == 2:
            return 1.0
        if price_int in {1, 3}:
            return 0.75
        return 0.25

    if intent.budget == "expensive":
        if price_int >= 3:
            return 1.0
        if price_int == 2:
            return 0.45
        return 0.2

    return 0.5


def score_cuisine_match(candidate: Mapping[str, Any], intent: MagicIntent) -> float:
    if not intent.cuisines:
        return 0.5

    text = _normalised_text(candidate)
    candidate_cuisine = str(
        candidate.get("cuisine_primary") or candidate.get("cuisine") or ""
    ).lower()
    for cuisine in intent.cuisines:
        if cuisine == candidate_cuisine or _contains_any(text, {cuisine}):
            return 1.0
    return 0.0


def score_vibe_match(candidate: Mapping[str, Any], intent: MagicIntent) -> float:
    if not intent.vibe_tags:
        return _clamp(_float(candidate.get("vibe_score"), 0.5))

    text = _normalised_text(candidate)
    matches = 0.0
    total = 0

    for tag in intent.vibe_tags:
        total += 1
        if tag == "good_for_groups" and _bool(candidate.get("good_for_groups")) is True:
            matches += 1.0
            continue
        if tag == "outdoor_seating" and _bool(candidate.get("outdoor_seating")) is True:
            matches += 1.0
            continue
        if tag in {"formal", "fine_dining", "upscale"} and _int(candidate.get("price_level"), -1) >= 3:
            matches += 0.7
        keywords = _VIBE_KEYWORDS.get(tag, {tag.replace("_", " ")})
        if _contains_any(text, keywords):
            matches += 1.0

    if total == 0:
        return 0.5
    return _clamp(matches / total)


def score_occasion_match(candidate: Mapping[str, Any], intent: MagicIntent) -> float:
    if not intent.occasion:
        return 0.5

    if intent.occasion == "group" and _bool(candidate.get("good_for_groups")) is True:
        return 1.0
    if intent.occasion == "brunch" and _bool(candidate.get("serves_brunch")) is True:
        return 1.0

    text = _normalised_text(candidate)
    keywords = _OCCASION_KEYWORDS.get(
        intent.occasion,
        {intent.occasion.replace("_", " ")},
    )
    if _contains_any(text, keywords):
        return 1.0
    return 0.25


def score_dietary_match(candidate: Mapping[str, Any], intent: MagicIntent) -> float:
    if not intent.dietary:
        return 0.5

    text = _normalised_text(candidate)
    scores: List[float] = []
    for dietary in intent.dietary:
        if dietary in {"vegetarian", "vegan"}:
            serves_veg = _bool(candidate.get("serves_vegetarian_food"))
            if serves_veg is True:
                scores.append(1.0)
            elif serves_veg is False:
                scores.append(0.0)
            elif _contains_any(text, {dietary}):
                scores.append(0.8)
            else:
                scores.append(0.45)
            continue
        scores.append(1.0 if _contains_any(text, {dietary.replace("_", " ")}) else 0.45)
    return _clamp(sum(scores) / len(scores))


def score_negative_penalty(candidate: Mapping[str, Any], intent: MagicIntent) -> float:
    if not intent.negative_terms:
        return 0.0

    text = _normalised_text(candidate)
    penalty = 0.0
    reviews = _int(candidate.get("user_ratings_total"), 0)
    social_score = _float(candidate.get("social_score"), 0.0)
    app_score = _float(candidate.get("app_engagement_score"), 0.0)

    if "touristy" in intent.negative_terms:
        if reviews >= 1000 and social_score <= 0.0 and app_score <= 0.1:
            penalty += 0.35
        if _contains_any(text, {"tourist", "touristy"}):
            penalty += 0.35

    for term in intent.negative_terms:
        if term == "touristy":
            continue
        if _contains_any(text, {term.replace("_", " ")}):
            penalty += 0.25

    return _clamp(penalty)


def score_intent_match(candidate: Mapping[str, Any], intent: MagicIntent) -> Dict[str, float]:
    components: Dict[str, float] = {
        "cuisine": score_cuisine_match(candidate, intent),
        "vibe": score_vibe_match(candidate, intent),
        "budget": score_budget_match(candidate, intent),
        "occasion": score_occasion_match(candidate, intent),
        "dietary": score_dietary_match(candidate, intent),
    }

    active_scores: List[float] = []
    if intent.cuisines:
        active_scores.append(components["cuisine"])
    if intent.vibe_tags:
        active_scores.append(components["vibe"])
    if intent.budget:
        active_scores.append(components["budget"])
    if intent.occasion:
        active_scores.append(components["occasion"])
    if intent.dietary:
        active_scores.append(components["dietary"])

    if not active_scores:
        active_scores = list(components.values())

    penalty = score_negative_penalty(candidate, intent)
    components["negative_penalty"] = penalty
    components["overall"] = _clamp((sum(active_scores) / len(active_scores)) - penalty)
    return components


def _distance_score(candidate: Mapping[str, Any], request_radius_km: float) -> float:
    distance_km = _float(candidate.get("distance_km"), default=request_radius_km)
    radius = max(float(request_radius_km or 0.0), 1.0)
    return _clamp(1.0 - min(distance_km / radius, 1.0))


def _availability_score(candidate: Mapping[str, Any], intent: MagicIntent) -> float:
    open_now = _bool(candidate.get("open_now"))
    if open_now is True:
        return 1.0
    if open_now is False:
        return 0.0 if intent.strict_constraints.get("open_now") else 0.25
    if intent.time_context in {"open_now", "now", "tonight", "late_night"}:
        return 0.45
    return 0.6


def _novelty_score(candidate: Mapping[str, Any], intent: MagicIntent) -> float:
    reviews = _int(candidate.get("user_ratings_total"), 0)
    rating = _float(candidate.get("rating"), 0.0)
    app_score = max(
        _float(candidate.get("app_engagement_score"), 0.0),
        _float(candidate.get("video_insight_score"), 0.0),
        _float(candidate.get("social_score"), 0.0),
    )

    if reviews == 0 and app_score <= 0:
        base = 0.25
    elif rating >= 4.3 and 20 <= reviews <= 500:
        base = 0.85
    elif rating >= 4.5 and reviews < 1000:
        base = 0.65
    else:
        base = 0.35

    if "hidden_gem" in intent.vibe_tags:
        base += 0.15
    return _clamp(base + min(app_score * 0.15, 0.15))


def _agentic_web_score(candidate: Mapping[str, Any]) -> float:
    web_agent = candidate.get("web_agent")
    if not isinstance(web_agent, Mapping):
        return 0.0

    confidence = _clamp(_float(web_agent.get("confidence"), 0.0))
    claims = set(web_agent.get("source_claims") or [])
    claim_bonus = 0.0
    if "ai_hotspot" in claims or "social_buzz" in claims:
        claim_bonus += 0.02
    if "date_spot" in claims or "new_opening" in claims:
        claim_bonus += 0.01
    return min(0.08, confidence * 0.06 + claim_bonus)


def _source_metadata(candidate: Mapping[str, Any]) -> List[Dict[str, Any]]:
    metadata: List[Dict[str, Any]] = []
    web_agent = candidate.get("web_agent")
    if isinstance(web_agent, Mapping):
        citations = list(web_agent.get("citations") or [])
        first_citation = citations[0] if citations else {}
        metadata.append(
            {
                "source": "ai_agent",
                "label": "Suggested by Magic Search AI",
                "confidence": _clamp(_float(web_agent.get("confidence"), 0.0)),
                "url": first_citation.get("url"),
                "title": first_citation.get("title"),
                "detail": web_agent.get("reason"),
                "claims": list(web_agent.get("source_claims") or []),
            }
        )
    return metadata


def _source(candidate: Mapping[str, Any]) -> List[str]:
    source = ["Google"]
    if candidate.get("is_known_location", True):
        source.append("Pinit")
    if _float(candidate.get("social_score"), 0.0) > 0 or candidate.get("friend_saves"):
        source.append("Friends")
    if _float(candidate.get("video_insight_score"), 0.0) > 0:
        source.append("Video")
    if isinstance(candidate.get("web_agent"), Mapping):
        source.append("AI Agent")
    return source


def rerank_magic_candidates(
    candidates: List[Dict[str, Any]],
    *,
    intent: MagicIntent,
    request_radius_km: float,
) -> List[Dict[str, Any]]:
    """Apply intent-aware Magic Search scoring to known and Google-only candidates."""
    ranked: List[Dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        base_personalised_score = _clamp(_float(item.get("final_score"), 0.0))
        intent_scores = score_intent_match(item, intent)
        google_quality = _google_quality_score(item)
        distance = _distance_score(item, request_radius_km)
        availability = _availability_score(item, intent)
        novelty = _novelty_score(item, intent)
        agentic_web = _agentic_web_score(item)

        final_score = (
            0.35 * base_personalised_score
            + 0.25 * intent_scores["overall"]
            + 0.15 * google_quality
            + 0.10 * distance
            + 0.10 * availability
            + 0.05 * novelty
            + agentic_web
        )

        score_breakdown = {
            **intent_scores,
            "base_personalised": base_personalised_score,
            "google_quality": google_quality,
            "distance": distance,
            "availability": availability,
            "novelty": novelty,
            "agentic_web": agentic_web,
        }
        item["intent_matches"] = score_breakdown
        item["source"] = _source(item)
        item["source_metadata"] = _source_metadata(item)
        item["confidence"] = _clamp((final_score * 0.65) + (intent_scores["overall"] * 0.35))
        item["final_score"] = _clamp(final_score)
        item["quality_score"] = google_quality
        item["match_reasons"] = build_match_reasons(item, intent, score_breakdown)
        ranked.append(item)

    ranked.sort(key=lambda row: row.get("final_score", 0.0), reverse=True)
    for rank, item in enumerate(ranked, start=1):
        item["rank"] = rank
    return ranked
