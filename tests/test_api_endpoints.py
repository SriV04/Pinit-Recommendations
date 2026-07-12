from __future__ import annotations

import base64
from contextlib import contextmanager, ExitStack
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    from tests import install_optional_dependency_stubs
except Exception:  # pragma: no cover - supports direct file execution
    install_optional_dependency_stubs = None

if install_optional_dependency_stubs is not None:
    install_optional_dependency_stubs()

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from fastapi.testclient import TestClient

from pinit.api.proximal_api import app as api_app
from pinit.api.routers import proximal
from pinit.api.services.magic_google_service import MagicGoogleSearchResult
from pinit.worker import main as worker_main


USER_ID = "cb9c8a52-581b-466f-9a76-588532c4b5e9"
SECOND_USER_ID = "9fcb7f54-902d-4a61-bd92-a92d83d263ef"


def _vibe_vector(primary_index: int = 0) -> list[int]:
    values = [20] * 25
    values[primary_index] = 95
    values[(primary_index + 1) % 25] = 70
    return values


def _dietary_vector() -> list[int]:
    return [80, 20, 30, 70, 10, 10]


def _candidate(
    location_id: int,
    name: str,
    lat: float,
    lng: float,
    *,
    cuisine: str = "italian",
    quality_score: float = 0.82,
    reviews: int = 120,
    vibe_index: int = 0,
) -> dict:
    return {
        "location_id": location_id,
        "name": name,
        "vicinity": f"{name} Street",
        "cuisine_primary": cuisine,
        "rating": 4.6,
        "user_ratings_total": reviews,
        "price_level": 2,
        "lat": lat,
        "lng": lng,
        "vibe_vector": _vibe_vector(vibe_index),
        "dietary_requirement_vector": _dietary_vector(),
        "app_engagement_score": quality_score,
        "google_baseline_score": quality_score,
        "video_insight_score": 0.15,
        "share_count": 2,
        "has_app_signal": True,
        "quality_score": quality_score,
        "quality_bias": 0.0,
        "image_stored": True,
        "image_unavailable": False,
        "extra_photos_stored": 3,
    }


class _FakeCacheService:
    def __init__(self) -> None:
        self.profile_requests: list[str] = []
        self.cached_queries: list[tuple[float, float, float]] = []
        self.magic_intents: dict[str, dict] = {}

    def get_user_profile(self, user_id: str) -> dict | None:
        if user_id == "missing-user":
            return None
        return {
            "user_id": user_id,
            "vibe_vector": _vibe_vector(0),
            "dietary_vector": _dietary_vector(),
            "action_count": 10,
            "exists": True,
        }

    def get_or_build_user_profile(self, user_id: str, supabase) -> dict | None:
        self.profile_requests.append(user_id)
        if user_id == "missing-user":
            return None
        return {
            "vibe_vector": _vibe_vector(0),
            "dietary_vector": _dietary_vector(),
            "action_count": 10,
        }

    def get_cached_recommendations(
        self,
        latitude: float,
        longitude: float,
        radius_km: float,
    ) -> dict:
        self.cached_queries.append((latitude, longitude, radius_km))
        return {
            "center_lat": latitude,
            "center_lng": longitude,
            "candidates": [
                _candidate(1001, "Nearby Pasta", latitude, longitude, cuisine="italian"),
                _candidate(1002, "Nearby Cafe", latitude + 0.003, longitude + 0.003, cuisine="cafe", quality_score=0.68),
                _candidate(1003, "Far Away", latitude + 1.0, longitude + 1.0, cuisine="japanese"),
            ],
        }

    def set_cached_recommendations(self, *args, **kwargs) -> None:
        return None

    def get_magic_intent(self, normalised_prompt: str) -> dict | None:
        return self.magic_intents.get(normalised_prompt)

    def set_magic_intent(self, normalised_prompt: str, payload: dict) -> bool:
        self.magic_intents[normalised_prompt] = payload
        return True


class _FakeSupabase:
    vibe_tag_order = proximal.VIBE_TAG_ORDER

    def count_locations(self) -> int:
        return 42

    def count_users(self) -> int:
        return 7

    def count_tags(self) -> int:
        return 25

    def get_user(self, user_id: str) -> dict | None:
        if user_id == "missing-user":
            return None
        return {"user_id": user_id}

    def get_user_vectors(self, user_id: str) -> dict | None:
        if user_id == "missing-user":
            return None
        index = 0 if user_id == USER_ID else 4
        return {
            "vibe_tag_affinity": _vibe_vector(index),
            "dietary_requirement_tag_affinity": _dietary_vector(),
        }

    def get_user_action_count(self, user_id: str) -> int:
        return 12 if user_id == USER_ID else 8

    def get_recent_recommendation_timestamps(self, user_id: str, location_ids: list[int]) -> dict:
        return {}

    def get_locations_with_quality_scores(
        self,
        latitude: float,
        longitude: float,
        radius_km: float,
        *,
        limit: int = 1000,
    ) -> list[dict]:
        return [
            _candidate(2001, "Tiny Quality Gem", latitude, longitude, quality_score=0.92, reviews=38),
            _candidate(2002, "Popular Good Place", latitude + 0.002, longitude + 0.002, quality_score=0.91, reviews=900),
            _candidate(2003, "Too Few Reviews", latitude + 0.001, longitude + 0.001, quality_score=0.95, reviews=10),
        ]

    def get_location_by_google_place_id(self, google_place_id: str) -> dict | None:
        if google_place_id == "known-google-place":
            return {
                "location_id": 3001,
                "name": "Known Existing Place",
                "updated_vibe": True,
            }
        return None

    def get_location(self, location_id: int) -> dict | None:
        return {"location_id": location_id, "name": "Known Existing Place", "emoji": "🍜"}

    def get_locations_by_google_place_ids(self, google_place_ids: list[str]) -> dict:
        return {
            "known-place-id": {
                "location_id": 4001,
                "google_place_id": "known-place-id",
            }
        }

    def get_locations_by_ids(self, location_ids: list[int]) -> list[dict]:
        return [
            self._magic_known_row()
            for location_id in location_ids
            if location_id == 4001
        ]

    def _magic_known_row(self) -> dict:
        return {
            **_candidate(
                4001,
                "Magic Known Place",
                51.5095,
                -0.1490,
                cuisine="japanese",
                quality_score=0.88,
            ),
            "google_place_id": "known-place-id",
            "saves_count": 10,
            "dislikes_count": 0,
        }

    def get_locations_with_popularity_by_google_place_ids(
        self, google_place_ids: list[str]
    ) -> dict:
        if "known-place-id" in google_place_ids:
            return {"known-place-id": self._magic_known_row()}
        return {}

    def get_bubble_added_locations(self, bubble_id: str) -> list[dict]:
        return [
            {
                "location_id": 5001,
                "added_by": USER_ID,
                "vibe_vector": _vibe_vector(0),
            }
        ]


class _FailingHealthSupabase:
    def count_locations(self) -> int:
        raise RuntimeError("database unavailable")


class _TimeoutExactLocationCountSupabase:
    def count_locations(self) -> int:
        raise RuntimeError("canceling statement due to statement timeout")

    def count_locations_for_health(self) -> int:
        return 145321

    def count_users(self) -> int:
        return 7

    def count_tags(self) -> int:
        return 25


class _FakeDispatcher:
    dispatched: list[object] = []

    async def dispatch(self, payload) -> None:
        self.dispatched.append(payload)


def _pubsub_push_body(payload: dict, *, task_type: str = "pipeline") -> dict:
    raw = json.dumps(payload).encode("utf-8")
    return {
        "message": {
            "data": base64.b64encode(raw).decode("ascii"),
            "attributes": {"task_type": task_type},
            "messageId": "message-1",
        },
        "subscription": "projects/demo/subscriptions/location-tasks",
    }


class ProximalApiEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(api_app)
        self.supabase = _FakeSupabase()
        self.cache = _FakeCacheService()

    def _common_patches(self):
        return [
            patch.object(proximal, "get_supabase_service", return_value=self.supabase),
            patch.object(proximal, "get_cache_service", return_value=self.cache),
            patch("pinit.core.recommendation.social_scoring.compute_social_scores", return_value=({}, {})),
            patch("pinit.core.recommendation.collaborative_scoring.compute_collaborative_scores", return_value={}),
            patch("pinit.core.recommendation.bubble_recommendation.get_supabase_service", return_value=self.supabase),
        ]

    @contextmanager
    def _patched_integrations(self):
        with ExitStack() as stack:
            for patcher in self._common_patches():
                stack.enter_context(patcher)
            yield

    def test_root_returns_api_metadata(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Pinit Proximal Recommendations API")

    def test_health_reports_supabase_counts(self) -> None:
        with patch.object(proximal, "get_supabase_service", return_value=self.supabase):
            response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "healthy",
                "timestamp": response.json()["timestamp"],
                "data_loaded": True,
                "total_locations": 42,
                "total_users": 7,
                "total_tags": 25,
            },
        )

    def test_health_reports_unhealthy_when_supabase_fails(self) -> None:
        with patch.object(proximal, "get_supabase_service", return_value=_FailingHealthSupabase()):
            response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "unhealthy")
        self.assertFalse(response.json()["data_loaded"])

    def test_health_uses_timeout_safe_location_count(self) -> None:
        with patch.object(
            proximal,
            "get_supabase_service",
            return_value=_TimeoutExactLocationCountSupabase(),
        ):
            response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "healthy")
        self.assertTrue(response.json()["data_loaded"])
        self.assertEqual(response.json()["total_locations"], 145321)
        self.assertEqual(response.json()["total_users"], 7)
        self.assertEqual(response.json()["total_tags"], 25)

    def test_hidden_gems_returns_ranked_under_reviewed_locations(self) -> None:
        with patch.object(proximal, "get_supabase_service", return_value=self.supabase):
            response = self.client.get(
                "/hidden-gems",
                params={
                    "latitude": 51.5095,
                    "longitude": -0.1490,
                    "radius_km": 1.5,
                    "max_results": 5,
                    "min_reviews": 35,
                    "min_quality_score": 0.6,
                },
            )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["total_results"], 2)
        self.assertEqual(body["recommendations"][0]["name"], "Tiny Quality Gem")
        self.assertEqual(body["recommendations"][0]["rank"], 1)

    def test_proximal_recommendations_work_for_multiple_locations(self) -> None:
        locations = [
            (51.5095, -0.1490),
            (51.5155, -0.0922),
            (51.5245, -0.0739),
        ]

        with self._patched_integrations():
            for latitude, longitude in locations:
                with self.subTest(latitude=latitude, longitude=longitude):
                    response = self.client.post(
                        "/recommendations/proximal",
                        json={
                            "user_id": USER_ID,
                            "latitude": latitude,
                            "longitude": longitude,
                            "radius_km": 1.5,
                            "max_results": 20,
                        },
                    )

                    body = response.json()
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(body["user_id"], USER_ID)
                    self.assertEqual(body["center_lat"], latitude)
                    self.assertEqual(body["center_lon"], longitude)
                    self.assertGreaterEqual(body["total_results"], 1)
                    self.assertLessEqual(body["total_results"], 20)
                    self.assertEqual(body["recommendations"][0]["rank"], 1)
                    self.assertLessEqual(body["recommendations"][0]["distance_km"], 1.5)

    def test_proximal_recommendations_return_404_for_unknown_user(self) -> None:
        with patch.object(proximal, "get_supabase_service", return_value=self.supabase), patch.object(
            proximal,
            "get_cache_service",
            return_value=self.cache,
        ):
            response = self.client.post(
                "/recommendations/proximal",
                json={
                    "user_id": "missing-user",
                    "latitude": 51.5095,
                    "longitude": -0.1490,
                    "radius_km": 1.5,
                    "max_results": 20,
                },
            )

        self.assertEqual(response.status_code, 404)
        self.assertIn("missing-user", response.json()["detail"])

    def test_locations_add_existing_location_queues_pipeline(self) -> None:
        _FakeDispatcher.dispatched = []
        with patch.object(proximal, "get_supabase_service", return_value=self.supabase), patch.object(
            proximal,
            "get_pubsub_config",
            return_value=SimpleNamespace(enabled=False, project_id="", topic=""),
        ), patch.object(proximal, "InProcessDispatcher", _FakeDispatcher):
            response = self.client.post(
                "/locations/add",
                json={
                    "google_place_id": "known-google-place",
                    "source": "in-app",
                    "classify_photo": False,
                    "generate_emoji": False,
                },
            )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["success"])
        self.assertTrue(body["already_existed"])
        self.assertEqual(body["location_id"], 3001)
        self.assertEqual(len(_FakeDispatcher.dispatched), 1)
        self.assertEqual(_FakeDispatcher.dispatched[0].task_type, "pipeline")
        self.assertEqual(_FakeDispatcher.dispatched[0].location_id, 3001)

    def test_magic_search_ranks_food_places(self) -> None:
        google_result = MagicGoogleSearchResult(
            places=[
                {
                    "id": "known-place-id",
                    "types": ["restaurant"],
                },
                {
                    "id": "retail-place-id",
                    "types": ["store"],
                },
            ],
            total_google_calls=1,
            total_candidates_before_dedupe=2,
            total_candidates_after_dedupe=2,
            google_queries=["sushi restaurant"],
        )
        with self._patched_integrations(), patch.object(
            proximal,
            "get_or_fetch_google_candidates",
            new=AsyncMock(return_value=google_result),
        ):
            response = self.client.post(
                "/locations/magic-search",
                json={
                    "user_id": USER_ID,
                    "latitude": 51.5095,
                    "longitude": -0.1490,
                    "prompt": "date night sushi nearby",
                    "radius_km": 1.5,
                    "max_results": 5,
                },
            )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["prompt"], "date night sushi nearby")
        self.assertEqual(body["total_candidates"], 2)
        self.assertEqual(body["total_ranked"], 1)
        self.assertEqual(body["recommendations"][0]["name"], "Magic Known Place")
        self.assertIn("source", body["recommendations"][0])
        self.assertIn("match_reasons", body["recommendations"][0])
        self.assertIn("intent_matches", body["recommendations"][0])
        self.assertIn("confidence", body["recommendations"][0])
        self.assertGreaterEqual(len(body["sections"]), 1)
        self.assertEqual(body["sections"][0]["title"], "Best matches")
        # Sections are header-only; each recommendation carries its own section
        # header instead of being relisted inside the section.
        self.assertNotIn("recommendations", body["sections"][0])
        self.assertEqual(body["recommendations"][0]["section"], "Best matches")
        self.assertEqual(body["debug"]["total_google_calls"], 1)
        self.assertEqual(body["debug"]["total_candidates_before_dedupe"], 2)
        self.assertEqual(body["debug"]["total_candidates_after_dedupe"], 2)
        self.assertEqual(body["debug"]["total_ranked"], 1)
        self.assertTrue(body["debug"]["cache_hit_user_profile"])

    def test_magic_search_returns_unknown_places_without_inserting_them(self) -> None:
        places = [
            {
                "id": "known-place-id",
                "displayName": {"text": "Known Google Name"},
                "formattedAddress": "Known Street, London",
                "types": ["restaurant"],
                "location": {"latitude": 51.5095, "longitude": -0.1490},
                "rating": 4.5,
                "userRatingCount": 200,
                "websiteUri": "https://knownplace.example",
                "googleMapsUri": "https://maps.google.com/?cid=88",
                "editorialSummary": {"text": "Known place summary."},
                "reviewSummary": {"text": {"text": "Known review summary."}},
            },
            {
                "id": "new-place-id",
                "displayName": {"text": "New Google Place"},
                "shortFormattedAddress": "New Street",
                "formattedAddress": "1 New Street, London",
                "types": ["restaurant"],
                "location": {"latitude": 51.5100, "longitude": -0.1485},
                "rating": 4.8,
                "userRatingCount": 500,
                "priceLevel": "PRICE_LEVEL_MODERATE",
                "currentOpeningHours": {
                    "openNow": True,
                    "weekdayDescriptions": [
                        "Monday: 9 AM – 11 PM",
                        "Tuesday: 9 AM – 11 PM",
                    ],
                },
                "websiteUri": "https://newplace.example",
                "googleMapsUri": "https://maps.google.com/?cid=99",
                "internationalPhoneNumber": "+44 20 7946 0958",
                "businessStatus": "OPERATIONAL",
                "editorialSummary": {"text": "Cosy neighbourhood spot."},
                "reviewSummary": {"text": {"text": "Loved by locals."}},
                "goodForChildren": True,
                "outdoorSeating": True,
                "servesCocktails": True,
                "servesBreakfast": False,
                "liveMusic": False,
                "photos": [
                    {
                        "name": "places/new-place-id/photos/photo-1",
                        "widthPx": 1200,
                        "heightPx": 800,
                    }
                ],
            },
        ]
        google_result = MagicGoogleSearchResult(
            places=places,
            total_google_calls=1,
            total_candidates_before_dedupe=2,
            total_candidates_after_dedupe=2,
            google_queries=["sushi restaurant"],
        )
        with (
            self._patched_integrations(),
            patch.object(
                proximal,
                "get_or_fetch_google_candidates",
                new=AsyncMock(return_value=google_result),
            ),
            patch.object(
                proximal,
                "fetch_google_place_basic_details",
            ) as basic_details,
            patch.object(
                proximal,
                "refresh_location_from_google_place_details",
            ) as full_details,
            patch.object(
                proximal,
                "create_location_from_place_details",
            ) as create_location,
        ):
            response = self.client.post(
                "/locations/magic-search",
                json={
                    "user_id": USER_ID,
                    "latitude": 51.5095,
                    "longitude": -0.1490,
                    "prompt": "date night sushi nearby",
                    "radius_km": 1.5,
                    "max_results": 5,
                },
            )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["total_candidates"], 2)
        self.assertEqual(body["total_ranked"], 2)
        recommendations = body["recommendations"]
        self.assertEqual(
            {item["google_place_id"] for item in recommendations},
            {"known-place-id", "new-place-id"},
        )
        known = next(item for item in recommendations if item["google_place_id"] == "known-place-id")
        self.assertTrue(known["is_known_location"])
        self.assertEqual(known["website"], "https://knownplace.example")
        self.assertEqual(known["google_maps_uri"], "https://maps.google.com/?cid=88")
        self.assertEqual(known["editorial_summary"], "Known place summary.")
        self.assertEqual(known["review_summary"], "Known review summary.")

        unknown = next(item for item in recommendations if item["google_place_id"] == "new-place-id")
        self.assertLess(unknown["location_id"], 0)
        self.assertFalse(unknown["is_known_location"])
        self.assertEqual(unknown["name"], "New Google Place")
        self.assertEqual(unknown["photo_reference"], "places/new-place-id/photos/photo-1")
        self.assertAlmostEqual(unknown["lat"], 51.5100)
        self.assertAlmostEqual(unknown["lng"], -0.1485)
        self.assertEqual(unknown["website"], "https://newplace.example")
        self.assertEqual(unknown["google_maps_uri"], "https://maps.google.com/?cid=99")
        self.assertEqual(unknown["editorial_summary"], "Cosy neighbourhood spot.")
        self.assertEqual(unknown["review_summary"], "Loved by locals.")
        self.assertEqual(unknown["international_phone_number"], "+44 20 7946 0958")
        self.assertEqual(unknown["business_status"], "OPERATIONAL")
        self.assertEqual(unknown["formatted_address"], "1 New Street, London")
        self.assertEqual(
            unknown["opening_hours_text"],
            ["Monday: 9 AM – 11 PM", "Tuesday: 9 AM – 11 PM"],
        )
        self.assertTrue(unknown["good_for_children"])
        self.assertTrue(unknown["outdoor_seating"])
        self.assertTrue(unknown["serves_cocktails"])
        self.assertFalse(unknown["serves_breakfast"])
        self.assertFalse(unknown["live_music"])
        self.assertIn("Google", unknown["source"])
        self.assertGreaterEqual(len(unknown["match_reasons"]), 1)
        self.assertGreaterEqual(len(body["sections"]), 1)
        basic_details.assert_not_called()
        full_details.assert_not_called()
        create_location.assert_not_called()

    def test_magic_search_explicit_location_survives_user_coordinate_radius(self) -> None:
        google_result = MagicGoogleSearchResult(
            places=[
                {
                    "id": "known-place-id",
                    "displayName": {"text": "Hammersmith Formal"},
                    "types": ["restaurant"],
                    "location": {"latitude": 51.4927, "longitude": -0.2237},
                },
            ],
            total_google_calls=1,
            total_candidates_before_dedupe=1,
            total_candidates_after_dedupe=1,
            google_queries=["formal dinner restaurant in hammersmith river"],
        )
        hammersmith_candidate = {
            **_candidate(
                4001,
                "Hammersmith Formal",
                51.4927,
                -0.2237,
                cuisine="british",
                quality_score=0.88,
            ),
            "google_place_id": "known-place-id",
            "saves_count": 0,
            "dislikes_count": 0,
        }

        with self._patched_integrations(), patch.object(
            proximal,
            "get_or_fetch_google_candidates",
            new=AsyncMock(return_value=google_result),
        ), patch.object(
            self.supabase,
            "get_locations_with_popularity_by_google_place_ids",
            return_value={"known-place-id": hammersmith_candidate},
        ):
            response = self.client.post(
                "/locations/magic-search",
                json={
                    "user_id": USER_ID,
                    "latitude": 51.5095,
                    "longitude": -0.1490,
                    "prompt": "formal dinner in hammersmith river",
                    "radius_km": 2,
                    "max_results": 10,
                },
            )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["total_ranked"], 1)
        self.assertEqual(body["recommendations"][0]["name"], "Hammersmith Formal")

    def test_magic_search_keeps_camera_radius_for_non_area_location_phrases(self) -> None:
        google_result = MagicGoogleSearchResult(
            places=[
                {
                    "id": "known-place-id",
                    "types": ["restaurant"],
                    "location": {"latitude": 51.5095, "longitude": -0.1490},
                }
            ],
            total_google_calls=1,
            total_candidates_before_dedupe=1,
            total_candidates_after_dedupe=1,
            google_queries=["Avo toast in the sun"],
        )
        captured: dict[str, float] = {}

        def _capture_rank(**kwargs):
            captured["request_radius_km"] = kwargs["request_radius_km"]
            captured["filter_by_radius"] = kwargs["filter_by_radius"]
            ranked = []
            for candidate in kwargs["candidates"]:
                ranked.append(
                    {
                        **candidate,
                        "distance_km": 0.1,
                        "vibe_score": 0.0,
                        "dietary_score": 0.0,
                        "social_score": 0.0,
                        "collaborative_score": 0.0,
                        "final_score": 0.8,
                    }
                )
            return ranked

        with (
            self._patched_integrations(),
            patch.object(
                proximal,
                "get_or_fetch_google_candidates",
                new=AsyncMock(return_value=google_result),
            ),
            patch.object(proximal, "_rank_cached_candidates", side_effect=_capture_rank),
        ):
            response = self.client.post(
                "/locations/magic-search",
                json={
                    "user_id": USER_ID,
                    "latitude": 50.8225,
                    "longitude": -0.1372,
                    "prompt": "Avo toast in the sun",
                    "radius_km": 1.5,
                    "max_results": 5,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["request_radius_km"], 1.5)
        self.assertIs(captured["filter_by_radius"], False)

    def test_magic_search_does_not_cap_or_filter_near_me_cuisine_queries(self) -> None:
        # Magic search no longer caps the radius or drops far candidates: a
        # "near me" cuisine query keeps the requested radius and returns both
        # the near and far venues.
        google_result = MagicGoogleSearchResult(
            places=[
                {
                    "id": "near-uyghur-id",
                    "displayName": {"text": "Near Uyghur Kitchen"},
                    "types": ["restaurant"],
                    "location": {"latitude": 51.5095, "longitude": -0.1490},
                    "rating": 4.7,
                    "userRatingCount": 120,
                },
                {
                    "id": "far-uyghur-id",
                    "displayName": {"text": "Far Uyghur Kitchen"},
                    "types": ["restaurant"],
                    "location": {"latitude": 51.5095, "longitude": -0.7000},
                    "rating": 4.9,
                    "userRatingCount": 900,
                },
            ],
            total_google_calls=1,
            total_candidates_before_dedupe=2,
            total_candidates_after_dedupe=2,
            google_queries=["authentic Uyghur cuisine near me"],
        )
        captured: dict[str, float] = {}

        async def _capture_google_candidates(*args, **kwargs):
            captured["google_radius_km"] = kwargs["radius_km"]
            return google_result

        with (
            self._patched_integrations(),
            patch.object(
                proximal,
                "get_or_fetch_google_candidates",
                new=AsyncMock(side_effect=_capture_google_candidates),
            ),
        ):
            response = self.client.post(
                "/locations/magic-search",
                json={
                    "user_id": USER_ID,
                    "latitude": 51.5095,
                    "longitude": -0.1490,
                    "prompt": "authentic Uyghur cuisine near me",
                    "radius_km": 50,
                    "max_results": 5,
                },
            )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["google_radius_km"], 50.0)
        self.assertEqual(body["radius_km"], 50.0)
        self.assertEqual(
            sorted(item["name"] for item in body["recommendations"]),
            ["Far Uyghur Kitchen", "Near Uyghur Kitchen"],
        )

    def test_bubble_recommendations_return_group_results(self) -> None:
        with self._patched_integrations():
            response = self.client.post(
                "/recommendations/bubble",
                json={
                    "user_ids": [USER_ID, SECOND_USER_ID],
                    "bubble_id": "bubble-123",
                    "latitude": 51.5095,
                    "longitude": -0.1490,
                    "radius_km": 1.5,
                    "max_results": 10,
                },
            )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["group_size"], 2)
        self.assertGreaterEqual(body["total_results"], 1)
        self.assertEqual(body["recommendations"][0]["rank"], 1)
        self.assertEqual(
            {score["user_id"] for score in body["recommendations"][0]["individual_vibe_scores"]},
            {USER_ID, SECOND_USER_ID},
        )


class WorkerApiEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(worker_main.app)

    def test_worker_health_returns_ok(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_worker_pubsub_handler_acks_valid_location_task(self) -> None:
        payload = {
            "task_type": "pipeline",
            "request_id": "request-1",
            "location_id": 7001,
            "google_place_id": "google-place-1",
            "source": "in-app",
            "generate_emoji": False,
            "classify_photo": False,
            "created_new": False,
        }
        handler = AsyncMock()

        with patch.object(worker_main, "handle_location_task", handler):
            response = self.client.post(
                "/internal/pubsub/location-tasks",
                json=_pubsub_push_body(payload),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        handler.assert_awaited_once()
        handled_payload = handler.await_args.args[0]
        self.assertEqual(handled_payload.task_type, "pipeline")
        self.assertEqual(handled_payload.location_id, 7001)

    def test_worker_pubsub_handler_rejects_invalid_json(self) -> None:
        response = self.client.post(
            "/internal/pubsub/location-tasks",
            data="{not-json",
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid JSON", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
