import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests import install_optional_dependency_stubs

install_optional_dependency_stubs()

from pinit.api.services.cache_service import ProximalCacheService
from pinit.config.settings import CacheConfig

V6_MIGRATION = REPO_ROOT / "supabase/migrations/20260427000000_v6_quality_score_bias.sql"
KNN_FIX_MIGRATION = REPO_ROOT / "supabase/migrations/20260427002000_optimize_get_locations_with_pillars_knn.sql"
FILL_LOCATIONS_MIGRATION = REPO_ROOT / "supabase/migrations/20260501170000_add_get_fill_locations_rpc.sql"
PROXIMAL_ROUTER = REPO_ROOT / "src/pinit/api/routers/proximal.py"


class _FakeRedis:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = values
        self.requested_keys: list[str] = []

    def ping(self) -> bool:
        return True

    def get(self, key: str) -> bytes | None:
        self.requested_keys.append(key)
        return self.values.get(key)


class SupabaseRpcContractTests(unittest.TestCase):
    def test_v6_quality_bias_rpc_uses_real_vibe_vectors(self) -> None:
        sql = V6_MIGRATION.read_text()
        self.assertIn("vibe_vector REAL[]", sql)

    def test_v6_quality_bias_rpc_uses_knn_ordering(self) -> None:
        sql = V6_MIGRATION.read_text()
        self.assertIn("ORDER BY lp.geog <-> center.g", sql)
        self.assertIn("ORDER BY l.geog <-> center.g", sql)
        self.assertNotIn("SELECT\n            l.*", sql)

    def test_knn_migrations_avoid_variable_limit_expressions(self) -> None:
        for path in (V6_MIGRATION, KNN_FIX_MIGRATION):
            sql = path.read_text()
            self.assertNotIn("LIMIT GREATEST(result_limit - pc.count, 0)", sql)
            self.assertIn("ROW_NUMBER() OVER", sql)

    def test_fill_locations_rpc_matches_cache_warming_contract(self) -> None:
        sql = FILL_LOCATIONS_MIGRATION.read_text()
        self.assertIn("CREATE OR REPLACE FUNCTION public.get_fill_locations", sql)
        self.assertIn("result_limit INT DEFAULT 1000", sql)
        self.assertIn("vibe_vector REAL[]", sql)
        self.assertIn("quality_bias NUMERIC", sql)
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("ROW_NUMBER() OVER", sql)
        self.assertIn("ORDER BY l.geog <-> center.g", sql)
        self.assertIn("FALSE AS has_app_signal", sql)
        self.assertIn("TO authenticated, anon, service_role", sql)

    def test_proximal_cache_payload_preserves_quality_bias(self) -> None:
        source = PROXIMAL_ROUTER.read_text()
        match = re.search(r"cache_cols = \[(.*?)\n\s*\]", source, re.DOTALL)
        self.assertIsNotNone(match, "cache_cols list not found in proximal router")
        cache_cols_block = match.group(1)
        for required_column in (
            "'app_engagement_score'",
            "'google_baseline_score'",
            "'video_insight_score'",
            "'share_count'",
            "'has_app_signal'",
            "'quality_score'",
            "'quality_bias'",
        ):
            self.assertIn(required_column, cache_cols_block)

    def test_lpa_snapshot_falls_back_to_legacy_key(self) -> None:
        service = ProximalCacheService.__new__(ProximalCacheService)
        service.config = CacheConfig(caching_enabled=True)
        payload = {"candidates": [{"location_id": 123}], "cached_at": "2026-04-27T21:00:00"}
        raw = ProximalCacheService._compress_data(service, payload)
        fake_redis = _FakeRedis({"lpa:snapshot:v2": raw})
        service._redis_client = fake_redis

        rows = service.get_lpa_snapshot()

        self.assertEqual(rows, payload["candidates"])
        self.assertEqual(fake_redis.requested_keys[:2], ["lpa:snapshot:v3", "lpa:snapshot:v2"])


if __name__ == "__main__":
    unittest.main()
