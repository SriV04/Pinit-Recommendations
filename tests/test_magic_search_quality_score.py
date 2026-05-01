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

    def test_get_locations_by_ids_flattens_lpa_pillars_for_magic_search(self) -> None:
        service = SupabaseService.__new__(SupabaseService)
        service.client = _FakeClient()

        rows = service.get_locations_by_ids([101])

        self.assertIn("location_popularity_app", service.client.query.selected)
        self.assertEqual(rows[0]["app_engagement_score"], 0.72)
        self.assertEqual(rows[0]["google_baseline_score"], 0.48)
        self.assertEqual(rows[0]["video_insight_score"], 0.25)
        self.assertEqual(rows[0]["share_count"], 3)
        self.assertEqual(rows[0]["quality_bias"], 0.10)
        self.assertAlmostEqual(rows[0]["quality_score"], 0.68)


if __name__ == "__main__":
    unittest.main()
