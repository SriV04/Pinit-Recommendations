from __future__ import annotations

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
        "locationBias": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(radius_km * 1000.0),
            }
        },
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
