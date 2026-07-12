import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests import install_optional_dependency_stubs

install_optional_dependency_stubs()

from pinit.api.schemas import MagicLocationRecommendation, MagicSearchRequest
from pinit.api.schemas_magic import MagicSearchDebug


class MagicAgenticSchemaTests(unittest.TestCase):
    def test_request_can_enable_live_web_agent(self) -> None:
        request = MagicSearchRequest(
            user_id="user-1",
            latitude=51.5095,
            longitude=-0.1490,
            prompt="hot new date night spots in Soho",
            enable_live_web_agent=True,
        )

        self.assertTrue(request.enable_live_web_agent)

    def test_recommendation_can_expose_source_metadata(self) -> None:
        item = MagicLocationRecommendation(
            location_id=-1,
            google_place_id="gid-1",
            is_known_location=False,
            name="Test",
            vicinity="Soho",
            lat=51.51,
            lng=-0.13,
            distance_km=0.4,
            vibe_score=0.1,
            dietary_score=0.0,
            quality_score=0.8,
            social_score=0.0,
            collaborative_score=0.0,
            final_score=0.7,
            rank=1,
            source=["Google", "AI Agent"],
            source_metadata=[
                {
                    "source": "ai_agent",
                    "label": "Suggested by Magic Search AI",
                    "confidence": 0.74,
                    "url": "https://example.com/list",
                    "detail": "Listed as a current Soho date-night pick",
                }
            ],
        )

        self.assertEqual(item.source_metadata[0]["source"], "ai_agent")

    def test_debug_tracks_web_agent_metrics(self) -> None:
        debug = MagicSearchDebug(
            web_agent_latency_ms=123.0,
            web_agent_timed_out=False,
            web_agent_candidates=6,
            web_agent_supabase_hits=3,
            web_agent_resolved_candidates=1,
            web_agent_google_resolve_calls=2,
        )

        self.assertEqual(debug.web_agent_candidates, 6)
        self.assertEqual(debug.web_agent_supabase_hits, 3)
        self.assertEqual(debug.web_agent_resolved_candidates, 1)
        self.assertEqual(debug.web_agent_google_resolve_calls, 2)

    def test_response_can_include_cached_ai_enrichment_payload(self) -> None:
        from pinit.api.schemas_magic import MagicAIEnrichmentPayload

        payload = MagicAIEnrichmentPayload(
            source="xai_web_search",
            canonical_signature={"primary_intent": "date_spot"},
            generated_at="2026-05-31T12:00:00",
            expires_at="2026-06-01T12:00:00",
            cache_hit=True,
            items=[
                {
                    "google_place_id": "gid-1",
                    "name": "Test Place",
                    "place_resolution_query": "Test Place Soho London",
                    "reason": "Recent mention",
                    "confidence": 0.8,
                    "source_claims": ["ai_hotspot"],
                    "citations": [],
                    "matched_location_id": 123,
                    "match_status": "supabase_hit",
                }
            ],
        )

        self.assertTrue(payload.cache_hit)
        self.assertEqual(payload.items[0].google_place_id, "gid-1")


class MagicAgenticMergeTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_live_web_agent_does_not_call_ai_cache_helpers_or_enqueue(self) -> None:
        from pinit.api.routers import proximal

        request = MagicSearchRequest(
            user_id="user-1",
            latitude=51.5095,
            longitude=-0.1490,
            prompt="hot new date night spots in Soho",
            enable_live_web_agent=False,
        )

        with patch.object(proximal, "get_supabase_service") as get_supabase, \
             patch.object(proximal, "get_cache_service") as get_cache, \
             patch.object(proximal, "_get_or_parse_magic_intent") as parse_intent, \
             patch.object(proximal, "_get_magic_user_profile", new=AsyncMock(return_value=({"vibe_vector": []}, False))), \
             patch.object(proximal, "get_or_fetch_google_candidates", new=AsyncMock()) as google, \
             patch.object(proximal, "build_magic_ai_signature", create=True) as build_signature, \
             patch.object(proximal, "get_background_job_runner", create=True) as get_runner:
            supabase = get_supabase.return_value
            supabase.get_locations_by_google_place_ids.return_value = {}
            supabase.get_locations_by_ids.return_value = []
            supabase.vibe_tag_order = {}
            cache = get_cache.return_value
            parse_intent.return_value = (proximal.parse_magic_intent(request.prompt), False)
            google.return_value.places = []
            google.return_value.total_google_calls = 0
            google.return_value.cache_hit_google_search = False
            google.return_value.total_candidates_before_dedupe = 0
            google.return_value.total_candidates_after_dedupe = 0
            google.return_value.google_queries = []

            await proximal.magic_search(request)

        build_signature.assert_not_called()
        cache.build_magic_ai_area_key.assert_not_called()
        cache.get_magic_ai_area_results.assert_not_called()
        get_runner.assert_not_called()

    async def test_magic_search_returns_cached_ai_enrichment_without_enqueuing_refresh(self) -> None:
        from pinit.api.routers import proximal

        request = MagicSearchRequest(
            user_id="user-1",
            latitude=51.5095,
            longitude=-0.1490,
            prompt="hot new date night spots in Soho",
            enable_live_web_agent=True,
        )
        cached_payload = {
            "source": "xai_web_search",
            "canonical_signature": {"primary_intent": "date_spot"},
            "generated_at": "2026-05-31T12:00:00",
            "expires_at": "2026-06-01T12:00:00",
            "cache_hit": False,
            "items": [
                {
                    "google_place_id": "gid-1",
                    "name": "Test Place",
                    "place_resolution_query": "Test Place Soho London",
                    "reason": "Recent mention",
                    "confidence": 0.8,
                    "source_claims": ["ai_hotspot"],
                    "citations": [],
                    "matched_location_id": 123,
                    "match_status": "supabase_hit",
                }
            ],
        }

        with patch.object(proximal, "get_supabase_service") as get_supabase, \
             patch.object(proximal, "get_cache_service") as get_cache, \
             patch.object(proximal, "_get_or_parse_magic_intent") as parse_intent, \
             patch.object(proximal, "_get_magic_user_profile", new=AsyncMock(return_value=({"vibe_vector": []}, False))), \
             patch.object(proximal, "get_or_fetch_google_candidates", new=AsyncMock()) as google, \
             patch.object(proximal, "get_background_job_runner", create=True) as get_runner:
            cache = get_cache.return_value
            cache.build_magic_ai_area_key.return_value = "magic:ai_area:v2:test"
            cache.get_magic_ai_area_results.return_value = cached_payload
            supabase = get_supabase.return_value
            supabase.get_locations_by_google_place_ids.return_value = {}
            supabase.get_locations_by_ids.return_value = []
            supabase.vibe_tag_order = {}
            parse_intent.return_value = (proximal.parse_magic_intent(request.prompt), False)
            google.return_value.places = []
            google.return_value.total_google_calls = 0
            google.return_value.cache_hit_google_search = False
            google.return_value.total_candidates_before_dedupe = 0
            google.return_value.total_candidates_after_dedupe = 0
            google.return_value.google_queries = []

            response = await proximal.magic_search(request)

        self.assertIsNotNone(response.ai_enrichment)
        self.assertTrue(response.ai_enrichment.cache_hit)
        self.assertEqual(response.ai_enrichment.items[0].google_place_id, "gid-1")
        self.assertTrue(response.debug.ai_enrichment_cache_hit)
        self.assertEqual(response.debug.ai_enrichment_items, 1)
        get_runner.return_value.enqueue.assert_not_called()

    async def test_magic_search_enqueues_ai_enrichment_refresh_on_cache_miss(self) -> None:
        from pinit.api.routers import proximal

        request = MagicSearchRequest(
            user_id="user-1",
            latitude=51.5095,
            longitude=-0.1490,
            prompt="hot new date night spots in Soho",
            enable_live_web_agent=True,
        )

        with patch.object(proximal, "get_supabase_service") as get_supabase, \
             patch.object(proximal, "get_cache_service") as get_cache, \
             patch.object(proximal, "_get_or_parse_magic_intent") as parse_intent, \
             patch.object(proximal, "_get_magic_user_profile", new=AsyncMock(return_value=({"vibe_vector": []}, False))), \
             patch.object(proximal, "get_or_fetch_google_candidates", new=AsyncMock()) as google, \
             patch.object(proximal, "get_background_job_runner", create=True) as get_runner:
            cache = get_cache.return_value
            cache.build_magic_ai_area_key.return_value = "magic:ai_area:v2:test"
            cache.get_magic_ai_area_results.return_value = None
            runner = get_runner.return_value
            runner.enqueue = AsyncMock()
            supabase = get_supabase.return_value
            supabase.get_locations_by_google_place_ids.return_value = {}
            supabase.get_locations_by_ids.return_value = []
            supabase.vibe_tag_order = {}
            parse_intent.return_value = (proximal.parse_magic_intent(request.prompt), False)
            google.return_value.places = []
            google.return_value.total_google_calls = 0
            google.return_value.cache_hit_google_search = False
            google.return_value.total_candidates_before_dedupe = 0
            google.return_value.total_candidates_after_dedupe = 0
            google.return_value.google_queries = []

            response = await proximal.magic_search(request)

        self.assertIsNone(response.ai_enrichment)
        self.assertTrue(response.debug.ai_enrichment_refresh_enqueued)
        runner.enqueue.assert_awaited_once()
        name, handler = runner.enqueue.await_args.args
        self.assertEqual(name, "magic_ai_enrichment_refresh")
        self.assertTrue(callable(handler))


    async def test_ai_enrichment_suggestions_are_ranked_into_recommendations(self) -> None:
        from pinit.api.routers import proximal

        request = MagicSearchRequest(
            user_id="user-1",
            latitude=51.5095,
            longitude=-0.1490,
            prompt="hot new date night spots in Soho",
            enable_live_web_agent=True,
        )
        cached_payload = {
            "source": "xai_web_search",
            "canonical_signature": {"primary_intent": "date_spot"},
            "generated_at": "2026-05-31T12:00:00",
            "expires_at": "2026-06-01T12:00:00",
            "cache_hit": False,
            "items": [
                {
                    "google_place_id": "gid-ai-1",
                    "name": "Hidden Soho Wine Bar",
                    "place_resolution_query": "Hidden Soho Wine Bar London",
                    "formatted_address": "1 Soho St, London",
                    "lat": 51.5132,
                    "lng": -0.1340,
                    "reason": "Repeatedly called a buzzy new date spot",
                    "confidence": 0.9,
                    "source_claims": ["ai_hotspot", "date_spot"],
                    "citations": [{"url": "https://example.com", "title": "Best date bars"}],
                    "matched_location_id": None,
                    "match_status": "google_resolved",
                }
            ],
        }

        with patch.object(proximal, "get_supabase_service") as get_supabase, \
             patch.object(proximal, "get_cache_service") as get_cache, \
             patch.object(proximal, "_get_or_parse_magic_intent") as parse_intent, \
             patch.object(proximal, "_get_magic_user_profile", new=AsyncMock(return_value=({"vibe_vector": []}, False))), \
             patch.object(proximal, "get_or_fetch_google_candidates", new=AsyncMock()) as google, \
             patch.object(proximal, "get_background_job_runner", create=True) as get_runner:
            cache = get_cache.return_value
            cache.build_magic_ai_area_key.return_value = "magic:ai_area:v3:test"
            cache.get_magic_ai_area_results.return_value = cached_payload
            supabase = get_supabase.return_value
            supabase.get_locations_by_google_place_ids.return_value = {}
            supabase.get_locations_by_ids.return_value = []
            supabase.vibe_tag_order = {}
            parse_intent.return_value = (proximal.parse_magic_intent(request.prompt), False)
            google.return_value.places = []
            google.return_value.total_google_calls = 0
            google.return_value.cache_hit_google_search = False
            google.return_value.total_candidates_before_dedupe = 0
            google.return_value.total_candidates_after_dedupe = 0
            google.return_value.google_queries = []

            response = await proximal.magic_search(request)

        # The web-agent suggestion is ranked into recommendations even though
        # Google text search returned nothing.
        self.assertEqual(response.debug.ai_enrichment_ranked_candidates, 1)
        self.assertEqual(len(response.recommendations), 1)
        rec = response.recommendations[0]
        self.assertEqual(rec.google_place_id, "gid-ai-1")
        self.assertIn("AI Agent", rec.source)
        get_runner.return_value.enqueue.assert_not_called()


class MagicAIEnrichmentCandidateTests(unittest.TestCase):
    def test_candidate_carries_full_google_info_from_stored_place(self) -> None:
        from pinit.api.routers import proximal
        from pinit.api.schemas_magic import MagicAIEnrichmentItem

        item = MagicAIEnrichmentItem(
            name="The Set",
            place_resolution_query="The Set Brighton",
            reason="Buzzy new opening",
            confidence=0.92,
            source_claims=["new_opening"],
            match_status="google_resolved",
            google_place_id="gid-set",
            lat=50.83,
            lng=-0.14,
            formatted_address="9 Preston Rd, Brighton",
            google_place={
                "id": "gid-set",
                "displayName": {"text": "The Set"},
                "types": ["restaurant"],
                "location": {"latitude": 50.83, "longitude": -0.14},
                "rating": 4.7,
                "userRatingCount": 320,
                "photos": [{"name": "places/gid-set/photos/abc"}],
                "servesDinner": True,
                "currentOpeningHours": {"openNow": True},
            },
        )
        candidate = proximal._magic_ai_enrichment_candidate(
            item, request_lat=50.82, request_lng=-0.14
        )
        self.assertEqual(candidate["rating"], 4.7)
        self.assertEqual(candidate["user_ratings_total"], 320)
        self.assertEqual(candidate["photo_reference"], "places/gid-set/photos/abc")
        self.assertEqual(candidate["serves_dinner"], True)
        self.assertEqual(candidate["open_now"], True)
        # Confidence-seeded boost is still applied on top of the real data.
        self.assertEqual(candidate["final_score"], 0.92)

    def test_candidate_falls_back_to_lean_when_no_stored_place(self) -> None:
        from pinit.api.routers import proximal
        from pinit.api.schemas_magic import MagicAIEnrichmentItem

        item = MagicAIEnrichmentItem(
            name="Mystery Spot",
            place_resolution_query="Mystery Spot",
            reason="Heard about it",
            confidence=0.5,
            match_status="google_resolved",
            google_place_id="gid-x",
            lat=50.83,
            lng=-0.14,
        )
        candidate = proximal._magic_ai_enrichment_candidate(
            item, request_lat=50.82, request_lng=-0.14
        )
        self.assertIsNone(candidate["rating"])
        self.assertEqual(candidate["name"], "Mystery Spot")


class MagicSearchEffectiveCenterTests(unittest.TestCase):
    def test_center_follows_google_results_not_caller_gps(self) -> None:
        from pinit.api.routers import proximal

        # Caller is in London; Google found Brighton venues from the query text.
        brighton_places = [
            {"location": {"latitude": 50.812, "longitude": -0.102}},
            {"location": {"latitude": 50.824, "longitude": -0.138}},
            {"location": {"latitude": 50.821, "longitude": -0.140}},
        ]
        lat, lng = proximal._magic_search_effective_center(
            brighton_places, fallback_lat=51.4941, fallback_lng=-0.1738
        )
        # Median (~50.821) is re-centred onto Brighton and snapped to the
        # coarse grid so same-area searches share one cache key.
        self.assertEqual((lat, lng), (50.8, -0.1))
        self.assertLess(lat, 51.0)  # Brighton, not London

    def test_center_snaps_nearby_medians_to_the_same_cell(self) -> None:
        from pinit.api.routers import proximal

        # Two phrasings of a Brighton search return slightly different venue
        # sets (hence medians) but must collapse onto the same anchor.
        a = proximal._magic_search_effective_center(
            [{"location": {"latitude": 50.818, "longitude": -0.142}}],
            fallback_lat=0.0,
            fallback_lng=0.0,
        )
        b = proximal._magic_search_effective_center(
            [{"location": {"latitude": 50.831, "longitude": -0.137}}],
            fallback_lat=0.0,
            fallback_lng=0.0,
        )
        self.assertEqual(a, b)

    def test_center_falls_back_to_gps_when_no_places(self) -> None:
        from pinit.api.routers import proximal

        lat, lng = proximal._magic_search_effective_center(
            [], fallback_lat=51.4941, fallback_lng=-0.1738
        )
        self.assertEqual((lat, lng), (51.5, -0.2))


class MagicAIEnrichmentNegativeCacheTests(unittest.IsolatedAsyncioTestCase):
    async def _run_refresh(self, items):
        from pinit.api.routers import proximal

        cache = MagicMock()
        with patch.object(
            proximal,
            "build_magic_ai_enrichment_payload",
            new=AsyncMock(return_value={"items": items}),
        ):
            await proximal._refresh_magic_ai_enrichment_cache(
                cache_key="magic:ai_area:v3:test",
                prompt="hot new restaurants in Brighton",
                lat=50.82,
                lng=-0.14,
                radius_km=2.0,
                signature={"freshness": "recent"},
                user_profile={"vibe_vector": []},
                supabase=MagicMock(),
                cache_service=cache,
            )
        return cache

    async def test_empty_result_is_cached_with_short_ttl(self) -> None:
        cache = await self._run_refresh([])
        _, kwargs = cache.set_magic_ai_area_results.call_args
        self.assertEqual(kwargs["ttl_seconds"], 600)

    async def test_populated_result_uses_default_ttl(self) -> None:
        cache = await self._run_refresh([{"name": "X"}])
        _, kwargs = cache.set_magic_ai_area_results.call_args
        self.assertIsNone(kwargs["ttl_seconds"])


if __name__ == "__main__":
    unittest.main()
