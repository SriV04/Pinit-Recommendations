from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from pinit.api.services.magic_google_service import resolve_magic_web_agent_suggestions
from pinit.api.services.magic_web_agent import fetch_magic_web_agent_suggestions


FetchSuggestions = Callable[..., Awaitable[Any]]
ResolveSuggestions = Callable[..., Awaitable[List[Dict[str, Any]]]]


def _coerce_suggestions(result: Any) -> List[Dict[str, Any]]:
    suggestions = result.suggestions if hasattr(result, "suggestions") else result
    return [dict(item) for item in list(suggestions or [])]


def _display_name(place: Optional[Dict[str, Any]], fallback: str) -> str:
    display = (place or {}).get("displayName") or {}
    return str(display.get("text") or fallback or "").strip()


def _location_value(place: Optional[Dict[str, Any]], key: str) -> Optional[float]:
    value = ((place or {}).get("location") or {}).get(key)
    return float(value) if value is not None else None


def _match_key(suggestion: Dict[str, Any]) -> str:
    resolution_query = str(suggestion.get("place_resolution_query") or "").strip()
    if resolution_query:
        return f"query:{resolution_query.lower()}"
    name = str(suggestion.get("name") or "").strip()
    address_hint = str(suggestion.get("address_hint") or "").strip()
    return f"name:{name.lower()}|{address_hint.lower()}"


def _place_match_key(place: Dict[str, Any]) -> str:
    return _match_key(dict(place.get("web_agent") or {}))


def _normalise_item(
    *,
    suggestion: Dict[str, Any],
    place: Optional[Dict[str, Any]],
    matched_location_id: Optional[int],
) -> Dict[str, Any]:
    google_place_id = str((place or {}).get("id") or "").strip() or None
    return {
        "google_place_id": google_place_id,
        "name": _display_name(place, str(suggestion.get("name") or "")),
        "place_resolution_query": suggestion.get("place_resolution_query"),
        "address_hint": suggestion.get("address_hint"),
        "neighbourhood_hint": suggestion.get("neighbourhood_hint"),
        "formatted_address": (place or {}).get("formattedAddress"),
        "lat": _location_value(place, "latitude"),
        "lng": _location_value(place, "longitude"),
        "reason": suggestion.get("reason"),
        "confidence": suggestion.get("confidence"),
        "source_claims": list(suggestion.get("source_claims") or []),
        "citations": list(suggestion.get("citations") or []),
        "matched_location_id": matched_location_id,
        "match_status": (
            "supabase_hit"
            if matched_location_id is not None
            else ("google_resolved" if google_place_id else "unresolved")
        ),
    }


async def build_magic_ai_enrichment_payload(
    *,
    prompt: str,
    lat: float,
    lng: float,
    radius_km: float,
    signature: Dict[str, Any],
    user_profile: Optional[Dict[str, Any]],
    supabase: Any,
    max_candidates: int = 8,
    fetch_suggestions: FetchSuggestions = fetch_magic_web_agent_suggestions,
    resolve_suggestions: ResolveSuggestions = resolve_magic_web_agent_suggestions,
) -> Dict[str, Any]:
    generated_at = datetime.now(timezone.utc)
    result = await fetch_suggestions(
        prompt=prompt,
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        user_profile=user_profile,
        vibe_tag_order=getattr(supabase, "vibe_tag_order", {}),
        cache=None,
        max_candidates=max_candidates,
    )
    suggestions = _coerce_suggestions(result)
    suggestions_for_resolution = [
        {key: value for key, value in suggestion.items() if key != "google_place_id"}
        for suggestion in suggestions
    ]
    places = await resolve_suggestions(
        suggestions_for_resolution,
        known_google_place_ids=set(),
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        max_google_resolves=max_candidates,
    )

    place_by_match_key = {
        _place_match_key(place): place
        for place in places
        if _place_match_key(place)
    }
    google_ids = [
        str(place.get("id") or "").strip()
        for place in places
        if str(place.get("id") or "").strip()
    ]
    existing_by_gid = (
        supabase.get_locations_by_google_place_ids(google_ids)
        if google_ids
        else {}
    )

    items: List[Dict[str, Any]] = []
    for suggestion in suggestions:
        place = place_by_match_key.get(_match_key(suggestion))
        google_place_id = str((place or {}).get("id") or "").strip()
        location = existing_by_gid.get(google_place_id) if google_place_id else None
        raw_location_id = (location or {}).get("location_id")
        matched_location_id = int(raw_location_id) if raw_location_id else None
        items.append(
            _normalise_item(
                suggestion=suggestion,
                place=place,
                matched_location_id=matched_location_id,
            )
        )

    return {
        "source": "xai_web_search",
        "canonical_signature": signature,
        "generated_at": generated_at.isoformat(),
        "expires_at": (generated_at + timedelta(hours=24)).isoformat(),
        "items": items,
    }
