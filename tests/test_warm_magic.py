import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests import install_optional_dependency_stubs

install_optional_dependency_stubs()

from pinit.cli import warm_magic


class WarmMagicTests(unittest.IsolatedAsyncioTestCase):
    def _cache(self, existing=None):
        cache = MagicMock()
        cache.is_available = True
        cache.build_magic_ai_area_key.return_value = "magic:ai_area:v4:test"
        cache.get_magic_ai_area_results.return_value = existing
        return cache

    async def test_skips_already_warm_entry(self) -> None:
        cache = self._cache(existing={"items": [{"name": "X"}]})
        with patch.object(
            warm_magic, "build_magic_ai_enrichment_payload", new=AsyncMock()
        ) as build:
            did = await warm_magic.warm_magic_zone(
                lat=51.5095, lng=-0.149, prompt="restaurants near me",
                cache=cache, supabase=MagicMock(),
            )
        self.assertFalse(did)
        build.assert_not_called()
        cache.set_magic_ai_area_results.assert_not_called()

    async def test_refreshes_cold_entry_and_caches_full_ttl(self) -> None:
        cache = self._cache(existing=None)
        with patch.object(
            warm_magic,
            "build_magic_ai_enrichment_payload",
            new=AsyncMock(return_value={"items": [{"name": "The Set"}]}),
        ):
            did = await warm_magic.warm_magic_zone(
                lat=51.5095, lng=-0.149, prompt="restaurants near me",
                cache=cache, supabase=MagicMock(),
            )
        self.assertTrue(did)
        _, kwargs = cache.set_magic_ai_area_results.call_args
        self.assertIsNone(kwargs["ttl_seconds"])  # populated => default TTL

    async def test_empty_refresh_uses_short_ttl(self) -> None:
        cache = self._cache(existing={"items": []})  # negative-cached / empty
        with patch.object(
            warm_magic,
            "build_magic_ai_enrichment_payload",
            new=AsyncMock(return_value={"items": []}),
        ):
            did = await warm_magic.warm_magic_zone(
                lat=51.5095, lng=-0.149, prompt="restaurants near me",
                cache=cache, supabase=MagicMock(),
            )
        self.assertTrue(did)
        _, kwargs = cache.set_magic_ai_area_results.call_args
        self.assertEqual(kwargs["ttl_seconds"], 600)

    async def test_pass_respects_max_refreshes_cap(self) -> None:
        cache = self._cache(existing=None)
        with patch.object(warm_magic, "get_cache_service", return_value=cache), \
             patch.object(warm_magic, "get_supabase_service", return_value=MagicMock()), \
             patch.object(
                 warm_magic,
                 "warm_magic_zone",
                 new=AsyncMock(return_value=True),
             ) as zone:
            refreshed = await warm_magic.warm_magic_pass(
                zone_set="london", max_refreshes=3
            )
        self.assertEqual(refreshed, 3)
        self.assertEqual(zone.await_count, 3)


if __name__ == "__main__":
    unittest.main()
