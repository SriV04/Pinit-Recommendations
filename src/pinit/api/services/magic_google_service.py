from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from pinit.api.schemas_magic import MagicIntent
from pinit.api.services.cache_service import ProximalCacheService
from pinit.config.secrets import GOOGLE_PLACE_API_KEY

MAX_GOOGLE_TEXT_CALLS = 2
MAX_GOOGLE_CANDIDATES = 40
_FALLBACK_MIN_CANDIDATES = 15
_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
_FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.types",
        "places.formattedAddress",
        "places.shortFormattedAddress",
        "places.location",
        "places.businessStatus",
        "places.googleMapsUri",
        "places.photos",
        "places.rating",
        "places.userRatingCount",
        "places.priceLevel",
        "places.currentOpeningHours",
        "places.websiteUri",
        "places.reviewSummary",
        "places.editorialSummary",
        "places.internationalPhoneNumber",
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
    ]
)


@dataclass
class MagicGoogleSearchResult:
    places: List[Dict[str, Any]]
    total_google_calls: int = 0
    cache_hit_google_search: bool = False
    total_candidates_before_dedupe: int = 0
    total_candidates_after_dedupe: int = 0
    google_queries: List[str] = field(default_factory=list)


def _dedupe_places(places: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    deduped: List[Dict[str, Any]] = []
    for place in places:
        place_id = str(place.get("id") or "").strip()
        if not place_id or place_id in seen:
            continue
        seen.add(place_id)
        deduped.append(place)
        if len(deduped) >= MAX_GOOGLE_CANDIDATES:
            break
    return deduped


async def _fetch_text_search(
    *,
    query: str,
    intent: MagicIntent,
    lat: float,
    lng: float,
    radius_km: float,
    api_key: str,
    client: Any,
    timeout: float,
) -> List[Dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": _FIELD_MASK,
    }
    body: Dict[str, Any] = {
        "textQuery": query,
        "maxResultCount": 20,
    }
    if intent.location_rectangle:
        body["locationRestriction"] = {"rectangle": intent.location_rectangle}
    else:
        body["locationBias"] = {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(radius_km * 1000.0),
            }
        }
    if intent.included_types:
        body["includedType"] = intent.included_types[0]

    response = await client.post(
        _TEXT_SEARCH_URL,
        json=body,
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    return list(data.get("places") or [])


async def get_or_fetch_google_candidates(
    intent: MagicIntent,
    *,
    lat: float,
    lng: float,
    radius_km: float,
    cache: Optional[ProximalCacheService],
    api_key: Optional[str] = None,
    client: Any = None,
    timeout: float = 4.0,
) -> MagicGoogleSearchResult:
    key = api_key or GOOGLE_PLACE_API_KEY
    if not key:
        raise ValueError("GOOGLE_PLACE_API_KEY not set")

    queries = (intent.google_queries or [intent.cleaned_query])[:MAX_GOOGLE_TEXT_CALLS]
    all_places: List[Dict[str, Any]] = []
    google_calls = 0
    cache_lookups = 0
    cache_hits = 0
    executed_queries: List[str] = []
    close_client = False

    if client is None:
        client = httpx.AsyncClient(timeout=timeout)
        close_client = True

    try:
        for query in queries:
            executed_queries.append(query)
            cache_key = None
            cached_payload = None
            if cache is not None and hasattr(cache, "build_magic_google_text_key"):
                cache_key = cache.build_magic_google_text_key(
                    query,
                    lat=lat,
                    lng=lng,
                    radius_km=radius_km,
                    included_types=intent.included_types,
                    location_rectangle=intent.location_rectangle,
                )
                cache_lookups += 1
                cached_payload = cache.get_magic_google_results(cache_key)

            if cached_payload is not None:
                cache_hits += 1
                raw_places = list(cached_payload.get("raw_places") or [])
            else:
                raw_places = await _fetch_text_search(
                    query=query,
                    intent=intent,
                    lat=lat,
                    lng=lng,
                    radius_km=radius_km,
                    api_key=key,
                    client=client,
                    timeout=timeout,
                )
                google_calls += 1
                if cache_key is not None and cache is not None:
                    place_ids = [
                        str(place.get("id"))
                        for place in raw_places
                        if str(place.get("id") or "").strip()
                    ]
                    cache.set_magic_google_results(
                        cache_key,
                        {
                            "query": query,
                            "lat": lat,
                            "lng": lng,
                            "radius_km": radius_km,
                            "place_ids": place_ids,
                            "raw_places": raw_places,
                            "cached_at": datetime.utcnow().isoformat(),
                        },
                    )

            all_places.extend(raw_places)
            if len(_dedupe_places(all_places)) >= _FALLBACK_MIN_CANDIDATES:
                break
    finally:
        if close_client:
            await client.aclose()

    deduped = _dedupe_places(all_places)
    return MagicGoogleSearchResult(
        places=deduped,
        total_google_calls=google_calls,
        cache_hit_google_search=cache_lookups > 0 and cache_hits == cache_lookups,
        total_candidates_before_dedupe=len(all_places),
        total_candidates_after_dedupe=len(deduped),
        google_queries=executed_queries,
    )


async def resolve_magic_web_agent_suggestions(
    suggestions: List[Dict[str, Any]],
    *,
    known_google_place_ids: Optional[set[str]] = None,
    lat: float,
    lng: float,
    radius_km: float,
    api_key: Optional[str] = None,
    client: Any = None,
    timeout: float = 2.0,
    max_google_resolves: int = 4,
) -> List[Dict[str, Any]]:
    """Resolve web-agent suggestions that are not already known locally.

    Suggestions with Google Place IDs already present in Supabase should be
    passed via ``known_google_place_ids`` so they do not incur another Google
    Text Search call.
    """
    key = api_key or GOOGLE_PLACE_API_KEY
    if not key or not suggestions:
        return []

    known_ids = {
        str(item).strip()
        for item in (known_google_place_ids or set())
        if str(item).strip()
    }
    suggestions_to_resolve = [
        item
        for item in suggestions
        if str(item.get("google_place_id") or "").strip() not in known_ids
    ][:max_google_resolves]
    if not suggestions_to_resolve:
        return []

    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=timeout)
        close_client = True

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": _FIELD_MASK,
    }
    resolved: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    async def _resolve_one(suggestion: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        query = str(suggestion.get("place_resolution_query") or "").strip()
        if not query:
            name = str(suggestion.get("name") or "").strip()
            address_hint = str(suggestion.get("address_hint") or "").strip()
            query = f"{name} {address_hint}".strip()
        if not query:
            return None

        body: Dict[str, Any] = {
            "textQuery": query,
            "maxResultCount": 3,
            "locationBias": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": float(radius_km * 1000.0),
                }
            },
            "includedType": "restaurant",
        }
        response = await client.post(
            _TEXT_SEARCH_URL,
            json=body,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        places = list(response.json().get("places") or [])
        if not places:
            return None
        place = dict(places[0])
        place["web_agent"] = suggestion
        return place

    try:
        tasks = [_resolve_one(item) for item in suggestions_to_resolve]
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, Exception) or result is None:
                continue
            place_id = str(result.get("id") or "").strip()
            if not place_id or place_id in seen_ids:
                continue
            seen_ids.add(place_id)
            resolved.append(result)
        return resolved
    finally:
        if close_client:
            await client.aclose()
