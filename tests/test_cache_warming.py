from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tests import install_optional_dependency_stubs

install_optional_dependency_stubs()

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from pinit.cli import warm_cache
from pinit.api import main as api_main
from pinit.config.settings import CacheConfig


class CacheTtlConfigTests(unittest.TestCase):
    def test_default_unfiltered_cache_ttl_is_sixty_minutes(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(CacheConfig().unfiltered_cache_ttl, 3600)


class WarmCacheSchedulerTests(unittest.TestCase):
    def test_run_warm_cache_pass_uses_lock_and_runs_snapshot_then_zones(self) -> None:
        events: list[object] = []

        with patch.object(warm_cache, "acquire_warm_cache_lock", return_value=True), patch.object(
            warm_cache,
            "refresh_lpa_snapshot",
            side_effect=lambda: events.append("snapshot") or True,
        ), patch.object(
            warm_cache,
            "warm_zones",
            side_effect=lambda zone_set, radius_km: events.append((zone_set, radius_km)) or 11,
        ):
            result = warm_cache.run_warm_cache_pass(zone_set="london", radius_km=15.0)

        self.assertTrue(result)
        self.assertEqual(events, ["snapshot", ("london", 15.0)])

    def test_run_warm_cache_pass_skips_when_lock_is_held(self) -> None:
        with patch.object(warm_cache, "acquire_warm_cache_lock", return_value=False), patch.object(
            warm_cache,
            "refresh_lpa_snapshot",
        ) as refresh:
            result = warm_cache.run_warm_cache_pass(zone_set="london", radius_km=15.0)

        self.assertFalse(result)
        refresh.assert_not_called()

    def test_warm_window_is_9am_to_before_9pm(self) -> None:
        self.assertFalse(warm_cache.is_within_warm_cache_window(datetime(2026, 5, 1, 8, 59)))
        self.assertTrue(warm_cache.is_within_warm_cache_window(datetime(2026, 5, 1, 9, 0)))
        self.assertTrue(warm_cache.is_within_warm_cache_window(datetime(2026, 5, 1, 20, 59)))
        self.assertFalse(warm_cache.is_within_warm_cache_window(datetime(2026, 5, 1, 21, 0)))

    def test_warm_cache_loop_runs_every_fifteen_minutes_inside_window(self) -> None:
        sleeps: list[float] = []
        passes: list[str] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        asyncio.run(
            warm_cache.warm_cache_loop(
                zone_set="london",
                radius_km=15.0,
                interval_seconds=900,
                sleep_fn=fake_sleep,
                now_fn=lambda: datetime(2026, 5, 1, 10, 0),
                warm_pass_fn=lambda zone_set, radius_km: passes.append(zone_set) or True,
                max_passes=1,
            )
        )

        self.assertEqual(passes, ["london"])
        self.assertEqual(sleeps, [900])

    def test_warm_cache_loop_skips_outside_cost_window(self) -> None:
        sleeps: list[float] = []
        passes: list[str] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        asyncio.run(
            warm_cache.warm_cache_loop(
                zone_set="london",
                radius_km=15.0,
                interval_seconds=900,
                sleep_fn=fake_sleep,
                now_fn=lambda: datetime(2026, 5, 1, 22, 0),
                warm_pass_fn=lambda zone_set, radius_km: passes.append(zone_set) or True,
                max_passes=1,
            )
        )

        self.assertEqual(passes, [])
        self.assertEqual(sleeps, [900])


class WarmCacheApiStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_startup_starts_warm_cache_scheduler_when_enabled(self) -> None:
        created: list[object] = []

        class _FakeTask:
            def cancel(self) -> None:
                return None

        async def fake_loop() -> None:
            return None

        async def fake_start() -> None:
            return None

        loop_calls: list[dict] = []

        def fake_loop_factory(**kwargs):
            loop_calls.append(kwargs)
            return fake_loop()

        def fake_create_task(coro):
            created.append(coro)
            coro.close()
            return _FakeTask()

        with patch.dict(os.environ, {"WARM_CACHE_ENABLED": "true"}), patch.object(
            api_main,
            "warm_cache_loop",
            new=fake_loop_factory,
        ), patch.object(
            api_main.asyncio,
            "create_task",
            side_effect=fake_create_task,
        ), patch.object(
            api_main,
            "get_pubsub_config",
            return_value=type("Cfg", (), {"enabled": True, "project_id": "", "topic": ""})(),
        ), patch.object(
            api_main,
            "get_background_job_runner",
            return_value=type("Runner", (), {"start": fake_start})(),
        ):
            await api_main.startup_background_workers()

        self.assertEqual(len(created), 1)
        self.assertEqual(len(loop_calls), 1)
        self.assertEqual(loop_calls[0]["interval_seconds"], 900)
        self.assertIsNotNone(api_main._warm_cache_task)
        api_main._warm_cache_task = None


if __name__ == "__main__":
    unittest.main()
