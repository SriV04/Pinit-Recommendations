import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests import install_optional_dependency_stubs

install_optional_dependency_stubs()

from pinit.api.routers import proximal
from pinit.integrations.supabase import SupabaseService


class _FakeSupabase:
    vibe_tag_order = []


class _FakeResponse:
    data = [
        {
            "location_id": 101,
            "name": "Known Place",
            "location_popularity_app": [
                {
                    "saves_count": 9,
                    "dislikes_count": 2,
                    "been_to_count": 3,
                    "app_engagement_score": 0.72,
                    "google_baseline_score": 0.48,
                    "video_insight_score": 0.25,
                    "share_count": 3,
                    "quality_bias": 0.10,
                }
            ],
        }
    ]


class _FakeQuery:
    def __init__(self) -> None:
        self.selected = ""

    def select(self, columns: str) -> "_FakeQuery":
        self.selected = columns
        return self

    def in_(self, column: str, values: list[int]) -> "_FakeQuery":
        self.filtered_column = column
        self.filtered_values = values
        return self

    def execute(self) -> _FakeResponse:
        return _FakeResponse()


class _FakeClient:
    def __init__(self) -> None:
        self.query = _FakeQuery()

    def table(self, table_name: str) -> _FakeQuery:
        self.table_name = table_name
        return self.query


class MagicSearchQualityScoreTests(unittest.TestCase):
    def test_magic_search_action_adjustment_boosts_saves_and_penalises_dislikes(self) -> None:
        saved = {"final_score": 0.50, "saves_count": 8, "been_to_count": 1, "dislikes_count": 0}
        disliked = {"final_score": 0.50, "saves_count": 0, "been_to_count": 0, "dislikes_count": 4}

        self.assertGreater(
            proximal._magic_search_action_adjusted_score(saved),
            proximal._magic_search_action_adjusted_score(disliked),
        )
        self.assertGreater(proximal._magic_search_action_adjusted_score(saved), 0.50)
        self.assertLess(proximal._magic_search_action_adjusted_score(disliked), 0.50)

    def test_magic_search_external_candidate_uses_negative_id_and_google_baseline(self) -> None:
        candidate = proximal._magic_search_external_candidate(
            {
                "id": "google-new-place",
                "displayName": {"text": "New Place"},
                "shortFormattedAddress": "New Street",
                "types": ["restaurant"],
                "location": {"latitude": 51.501, "longitude": -0.121},
                "rating": 4.7,
                "userRatingCount": 240,
                "currentOpeningHours": {"openNow": True},
                "photos": [
                    {"name": "places/google-new-place/photos/photo-1"},
                ],
            },
            request_lat=51.500,
            request_lng=-0.120,
            search_position=0,
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertLess(candidate["location_id"], 0)
        self.assertEqual(candidate["google_place_id"], "google-new-place")
        self.assertEqual(candidate["name"], "New Place")
        self.assertFalse(candidate["is_known_location"])
        self.assertEqual(
            candidate["photo_reference"],
            "places/google-new-place/photos/photo-1",
        )
        self.assertGreater(candidate["quality_score"], 0)
        self.assertGreater(candidate["final_score"], 0)

    def test_ranked_candidates_include_quality_score_when_source_row_lacks_pillars(self) -> None:
        candidate = {
            "location_id": 101,
            "name": "Known Place",
            "lat": 51.5000,
            "lng": -0.1200,
            "rating": 4.5,
            "user_ratings_total": 120,
            "price_level": 2,
        }

        with patch.object(proximal, "get_supabase_service", return_value=_FakeSupabase()):
            ranked = proximal._rank_cached_candidates(
                candidates=[candidate],
                request_lat=51.5000,
                request_lng=-0.1200,
                request_radius_km=2.0,
                quality_weight=0.30,
                vibe_weight=0.25,
                dietary_weight=0.10,
                max_results=20,
                user_id=None,
            )

        self.assertEqual(len(ranked), 1)
        self.assertIn("quality_score", ranked[0])
        self.assertEqual(ranked[0]["quality_score"], 0.0)

    def test_ranked_candidates_can_keep_out_of_radius_magic_search_results(self) -> None:
        candidate = {
            "location_id": 202,
            "name": "Relevant Nepalese Restaurant",
            "lat": 51.5500,
            "lng": -0.1200,
            "rating": 4.5,
            "user_ratings_total": 120,
            "price_level": 2,
        }

        with patch.object(proximal, "get_supabase_service", return_value=_FakeSupabase()):
            ranked = proximal._rank_cached_candidates(
                candidates=[candidate],
                request_lat=51.5000,
                request_lng=-0.1200,
                request_radius_km=2.0,
                quality_weight=0.30,
                vibe_weight=0.25,
                dietary_weight=0.10,
                max_results=20,
                user_id=None,
                filter_by_radius=False,
            )

        self.assertEqual(len(ranked), 1)
        self.assertGreater(ranked[0]["distance_km"], 2.0)
        self.assertEqual(ranked[0]["location_id"], 202)

    def test_get_locations_by_ids_flattens_lpa_pillars_for_magic_search(self) -> None:
        service = SupabaseService.__new__(SupabaseService)
        service.client = _FakeClient()

        rows = service.get_locations_by_ids([101])

        self.assertIn("location_popularity_app", service.client.query.selected)
        self.assertIn("saves_count", service.client.query.selected)
        self.assertEqual(rows[0]["saves_count"], 9)
        self.assertEqual(rows[0]["dislikes_count"], 2)
        self.assertEqual(rows[0]["been_to_count"], 3)
        self.assertEqual(rows[0]["app_engagement_score"], 0.72)
        self.assertEqual(rows[0]["google_baseline_score"], 0.48)
        self.assertEqual(rows[0]["video_insight_score"], 0.25)
        self.assertEqual(rows[0]["share_count"], 3)
        self.assertEqual(rows[0]["quality_bias"], 0.10)
        self.assertAlmostEqual(rows[0]["quality_score"], 0.68)


if __name__ == "__main__":
    unittest.main()
