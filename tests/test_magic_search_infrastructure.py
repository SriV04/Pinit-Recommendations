import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests import install_optional_dependency_stubs

install_optional_dependency_stubs()

from pinit.api.services.cache_service import ProximalCacheService
from pinit.api.services.magic_google_service import (
    MAX_GOOGLE_TEXT_CALLS,
    get_or_fetch_google_candidates,
)
from pinit.api.services.magic_intent_parser import parse_magic_intent
from pinit.config.settings import CacheConfig


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}

    def ping(self) -> bool:
        return True

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def setex(self, key: str, ttl: int, value: bytes) -> bool:
        self.store[key] = value
        self.ttls[key] = ttl
        return True


class _FakeResponse:
    status_code = 200

    def __init__(self, places: list[dict]) -> None:
        self._places = places
        self.text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"places": self._places}


class _FakeAsyncClient:
    def __init__(self, batches: list[list[dict]]) -> None:
        self.batches = batches
        self.calls: list[dict] = []

    async def post(self, url: str, *, json: dict, headers: dict, timeout: float):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        batch = self.batches.pop(0)
        return _FakeResponse(batch)


class MagicSearchInfrastructureTests(unittest.TestCase):
    def _cache(self) -> ProximalCacheService:
        cache = ProximalCacheService.__new__(ProximalCacheService)
        cache.config = CacheConfig(caching_enabled=True, compression_enabled=True)
        cache._redis_client = _FakeRedis()
        return cache

    def test_parse_magic_intent_extracts_query_and_soft_signals(self) -> None:
        intent = parse_magic_intent("cheap vibey Thai for a group tonight")

        self.assertEqual(intent.raw_prompt, "cheap vibey Thai for a group tonight")
        self.assertEqual(intent.cleaned_query, "thai restaurant")
        self.assertEqual(intent.cuisines, ["thai"])
        self.assertEqual(intent.vibe_tags, ["trendy", "casual", "good_for_groups"])
        self.assertEqual(intent.occasion, "group")
        self.assertEqual(intent.budget, "cheap")
        self.assertEqual(intent.time_context, "tonight")
        self.assertEqual(intent.included_types, ["restaurant"])
        self.assertEqual(intent.google_queries, ["cheap vibey Thai for a group tonight"])
        self.assertIsNone(intent.location_rectangle)

    def test_parse_magic_intent_uses_area_rectangle_when_prompt_mentions_london_area(self) -> None:
        intent = parse_magic_intent("cheap sushi on old compton street")

        self.assertEqual(intent.google_queries, ["cheap sushi on old compton street"])
        self.assertEqual(
            intent.location_rectangle,
            {
                "low": {"latitude": 51.5090, "longitude": -0.1435},
                "high": {"latitude": 51.5175, "longitude": -0.1290},
            },
        )

    def test_parse_magic_intent_extracts_prompt_location(self) -> None:
        northolt = parse_magic_intent("Bougie Turkish in Northolt")
        london = parse_magic_intent("Bougie Turkish in London")

        self.assertEqual(northolt.location_name, "northolt")
        self.assertEqual(london.location_name, "london")
        self.assertEqual(northolt.cleaned_query, "turkish restaurant in northolt")
        self.assertEqual(london.cleaned_query, "turkish restaurant in london")
        self.assertNotEqual(northolt.google_queries, london.google_queries)
        self.assertEqual(northolt.google_queries, ["Bougie Turkish in Northolt"])
        self.assertEqual(london.google_queries, ["Bougie Turkish in London"])

    def test_parse_magic_intent_preserves_formal_dinner_modifier(self) -> None:
        intent = parse_magic_intent("formal dinner in hammersmith river")

        self.assertEqual(intent.location_name, "hammersmith river")
        self.assertEqual(intent.time_context, "dinner")
        self.assertIn("formal", intent.positive_terms)
        self.assertIn("dinner", intent.positive_terms)
        self.assertIn("formal", intent.vibe_tags)
        self.assertIn("fine_dining", intent.vibe_tags)
        self.assertEqual(
            intent.cleaned_query,
            "formal dinner restaurant in hammersmith river",
        )
        self.assertEqual(
            intent.google_queries[0],
            "formal dinner in hammersmith river",
        )

    def test_magic_cache_helpers_round_trip_compressed_payloads(self) -> None:
        cache = self._cache()

        intent_payload = {"intent": {"cleaned_query": "brunch restaurant"}}
        self.assertIsNone(cache.get_magic_intent("brunch"))

        self.assertTrue(cache.set_magic_intent("brunch", intent_payload))
        self.assertEqual(cache.get_magic_intent("brunch"), intent_payload)
        self.assertEqual(cache._redis_client.ttls["magic:intent:v4:brunch"], 7 * 24 * 60 * 60)

        google_key = cache.build_magic_google_text_key(
            "brunch restaurant",
            lat=51.5095,
            lng=-0.1490,
            radius_km=2.0,
            included_types=["restaurant"],
        )
        google_payload = {"place_ids": ["place-a"], "raw_places": [{"id": "place-a"}]}
        self.assertTrue(cache.set_magic_google_results(google_key, google_payload, ttl_seconds=6 * 60 * 60))
        self.assertEqual(cache.get_magic_google_results(google_key), google_payload)

        details_payload = {"google_place_id": "place-a", "rating": 4.5}
        self.assertTrue(cache.set_magic_place_details("place-a", details_payload))
        self.assertEqual(cache.get_magic_place_details("place-a"), details_payload)

        final_payload = {"recommendations": [], "debug": {"source": "cached_final_result"}}
        self.assertTrue(cache.set_magic_final_results("magic:results:v1:user:g_1_2:r2:intent", final_payload))
        self.assertEqual(
            cache.get_magic_final_results("magic:results:v1:user:g_1_2:r2:intent"),
            final_payload,
        )

    def test_magic_web_agent_cache_helpers_round_trip(self) -> None:
        cache = self._cache()

        key = cache.build_magic_web_agent_key(
            prompt="hot new date night spots in Soho",
            lat=51.5095,
            lng=-0.1490,
            radius_km=2.0,
            vibe_summary={"romantic": 0.91, "trendy": 0.77},
        )
        payload = {
            "suggestions": [{"name": "Test", "google_place_id": "gid-1"}],
            "cached_at": "2026-05-31T12:00:00",
        }

        self.assertTrue(cache.set_magic_web_agent_results(key, payload))
        self.assertEqual(cache.get_magic_web_agent_results(key), payload)
        self.assertTrue(key.startswith("magic:web_agent:v1:"))

    def test_magic_ai_area_cache_key_uses_signature_and_ttl(self) -> None:
        cache = self._cache()
        signature = {
            "geo_bucket": "51.494:-0.174",
            "radius_bucket": "2km",
            "intent_hash": "abc123",
            "freshness": "recent",
        }

        key = cache.build_magic_ai_area_key(signature)
        payload = {
            "items": [{"name": "Test", "google_place_id": "gid-1"}],
            "cached_at": "2026-05-31T12:00:00",
        }

        self.assertEqual(
            key,
            "magic:ai_area:v4:51.494:-0.174:2km:abc123:recent",
        )
        self.assertTrue(cache.set_magic_ai_area_results(key, payload))
        self.assertEqual(cache.get_magic_ai_area_results(key), payload)
        self.assertEqual(cache._redis_client.ttls[key], 24 * 60 * 60)


class MagicGoogleServiceTests(unittest.IsolatedAsyncioTestCase):
    def _cache(self) -> ProximalCacheService:
        cache = ProximalCacheService.__new__(ProximalCacheService)
        cache.config = CacheConfig(caching_enabled=True, compression_enabled=True)
        cache._redis_client = _FakeRedis()
        return cache

    async def test_async_google_fetch_caps_calls_dedupes_and_caches_results(self) -> None:
        cache = self._cache()
        intent = parse_magic_intent("cheap vibey Thai for a group tonight")
        intent.google_queries = ["thai restaurant", "casual thai restaurant", "thai food"]

        client = _FakeAsyncClient(
            [
                [
                    {"id": f"primary-{idx}", "types": ["restaurant"]}
                    for idx in range(5)
                ],
                [
                    {"id": "primary-0", "types": ["restaurant"]},
                    {"id": "fallback-1", "types": ["restaurant"]},
                ],
            ]
        )

        result = await get_or_fetch_google_candidates(
            intent,
            lat=51.5095,
            lng=-0.1490,
            radius_km=2.0,
            cache=cache,
            api_key="test-key",
            client=client,
        )

        self.assertEqual(len(client.calls), MAX_GOOGLE_TEXT_CALLS)
        self.assertEqual(result.total_google_calls, 2)
        self.assertEqual(result.total_candidates_before_dedupe, 7)
        self.assertEqual(result.total_candidates_after_dedupe, 6)
        self.assertEqual([place["id"] for place in result.places][-1], "fallback-1")
        self.assertFalse(result.cache_hit_google_search)

        cached_result = await get_or_fetch_google_candidates(
            intent,
            lat=51.5095,
            lng=-0.1490,
            radius_km=2.0,
            cache=cache,
            api_key="test-key",
            client=_FakeAsyncClient([]),
        )

        self.assertEqual(cached_result.total_google_calls, 0)
        self.assertEqual(cached_result.total_candidates_after_dedupe, 6)
        self.assertTrue(cached_result.cache_hit_google_search)

    async def test_async_google_result_reports_only_executed_queries_without_cache(self) -> None:
        intent = parse_magic_intent("cheap vibey Thai for a group tonight")
        intent.google_queries = ["thai restaurant", "casual thai restaurant"]
        client = _FakeAsyncClient(
            [
                [
                    {"id": f"primary-{idx}", "types": ["restaurant"]}
                    for idx in range(16)
                ],
            ]
        )

        result = await get_or_fetch_google_candidates(
            intent,
            lat=51.5095,
            lng=-0.1490,
            radius_km=2.0,
            cache=None,
            api_key="test-key",
            client=client,
        )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result.google_queries, ["thai restaurant"])

    async def test_async_google_fetch_uses_request_coordinates_when_no_london_area(self) -> None:
        intent = parse_magic_intent("quick coffee")
        client = _FakeAsyncClient(
            [
                [
                    {"id": f"primary-{idx}", "types": ["cafe"]}
                    for idx in range(16)
                ],
            ]
        )

        await get_or_fetch_google_candidates(
            intent,
            lat=50.8225,
            lng=-0.1372,
            radius_km=1.5,
            cache=None,
            api_key="test-key",
            client=client,
        )

        body = client.calls[0]["json"]
        self.assertNotIn("locationRestriction", body)
        self.assertEqual(
            body["locationBias"],
            {
                "circle": {
                    "center": {"latitude": 50.8225, "longitude": -0.1372},
                    "radius": 1500.0,
                }
            },
        )

    async def test_async_google_fetch_uses_raw_prompt_and_rectangle_restriction(self) -> None:
        intent = parse_magic_intent("cheap sushi on old compton street")
        client = _FakeAsyncClient(
            [
                [
                    {"id": f"primary-{idx}", "types": ["restaurant"]}
                    for idx in range(16)
                ],
            ]
        )

        result = await get_or_fetch_google_candidates(
            intent,
            lat=40.7128,
            lng=-74.0060,
            radius_km=2.0,
            cache=None,
            api_key="test-key",
            client=client,
        )

        self.assertEqual(result.google_queries, ["cheap sushi on old compton street"])
        body = client.calls[0]["json"]
        self.assertEqual(body["textQuery"], "cheap sushi on old compton street")
        self.assertEqual(
            body["locationRestriction"],
            {
                "rectangle": {
                    "low": {"latitude": 51.5090, "longitude": -0.1435},
                    "high": {"latitude": 51.5175, "longitude": -0.1290},
                }
            },
        )
        self.assertNotIn("locationBias", body)


if __name__ == "__main__":
    unittest.main()
