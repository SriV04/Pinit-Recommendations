from __future__ import annotations

import unittest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

try:
    from tests import install_optional_dependency_stubs
except Exception:  # pragma: no cover
    install_optional_dependency_stubs = None

if install_optional_dependency_stubs is not None:
    install_optional_dependency_stubs()

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from pinit.api.schemas_location_tasks import PipelinePayload, VibeReprocessPayload
from pinit.api.services import location_tasks


class _FakeDispatcher:
    def __init__(self) -> None:
        self.dispatched = []

    async def dispatch(self, payload) -> None:
        self.dispatched.append(payload)


class _FakeSupabase:
    def __init__(self, *, claimed: bool = True) -> None:
        self.claimed = claimed
        self.claim_calls = []
        self.clear_calls = []

    def claim_location_vibe_processing(self, location_id, request_id, *, stale_after_seconds):
        self.claim_calls.append((location_id, request_id, stale_after_seconds))
        return self.claimed

    def clear_location_vibe_processing(self, location_id, request_id):
        self.clear_calls.append((location_id, request_id))


class LocationTaskPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_magic_search_open_existing_location_runs_full_enrichment(self) -> None:
        dispatcher = _FakeDispatcher()
        payload = PipelinePayload(
            task_type="pipeline",
            request_id="request-1",
            location_id=1234,
            google_place_id="google-place-id",
            source="magic-search-open",
            generate_emoji=True,
            classify_photo=True,
            created_new=False,
        )

        with patch.object(
            location_tasks,
            "_get_location",
            return_value={"location_id": 1234, "updated_vibe": True},
        ):
            await location_tasks.pipeline_task(payload, dispatcher=dispatcher)

        self.assertEqual(len(dispatcher.dispatched), 1)
        self.assertEqual(dispatcher.dispatched[0].task_type, "details_enrich")
        self.assertTrue(dispatcher.dispatched[0].generate_emoji)
        self.assertTrue(dispatcher.dispatched[0].classify_photo)

    async def test_existing_social_location_with_updated_vibe_skips_vibe_reprocess(self) -> None:
        dispatcher = _FakeDispatcher()
        payload = PipelinePayload(
            task_type="pipeline",
            request_id="request-2",
            location_id=145558,
            google_place_id="google-place-id",
            source="instagram",
            generate_emoji=True,
            classify_photo=True,
            created_new=False,
        )

        with patch.object(
            location_tasks,
            "_get_location",
            return_value={
                "location_id": 145558,
                "updated_vibe": True,
            },
        ):
            await location_tasks.pipeline_task(payload, dispatcher=dispatcher)

        self.assertEqual(dispatcher.dispatched, [])

    async def test_existing_social_location_without_updated_vibe_dispatches_vibe_reprocess(self) -> None:
        dispatcher = _FakeDispatcher()
        payload = PipelinePayload(
            task_type="pipeline",
            request_id="request-3",
            location_id=145494,
            google_place_id="google-place-id",
            source="instagram",
            generate_emoji=True,
            classify_photo=True,
            created_new=False,
        )

        with patch.object(
            location_tasks,
            "_get_location",
            return_value={
                "location_id": 145494,
                "updated_vibe": False,
            },
        ):
            await location_tasks.pipeline_task(payload, dispatcher=dispatcher)

        self.assertEqual(len(dispatcher.dispatched), 1)
        self.assertEqual(dispatcher.dispatched[0].task_type, "vibe_reprocess")
        self.assertTrue(dispatcher.dispatched[0].force_blend)

    async def test_vibe_reprocess_skips_when_updated_vibe_true(self) -> None:
        payload = VibeReprocessPayload(
            task_type="vibe_reprocess",
            request_id="request-4",
            location_id=5987,
            google_place_id="google-place-id",
            source="tiktok",
            generate_emoji=True,
            classify_photo=True,
            created_new=False,
            force_blend=True,
        )
        generate_mock = AsyncMock(return_value=[0.0])

        with (
            patch.object(
                location_tasks,
                "_get_location",
                return_value={
                    "location_id": 5987,
                    "updated_vibe": True,
                },
            ),
            patch.object(location_tasks, "generate_vibe_tags_for_location", generate_mock),
        ):
            await location_tasks.vibe_reprocess_task(payload)

        generate_mock.assert_not_awaited()

    async def test_vibe_reprocess_without_updated_vibe_claims_lock_and_uses_three_runs(self) -> None:
        payload = VibeReprocessPayload(
            task_type="vibe_reprocess",
            request_id="request-5",
            location_id=5987,
            google_place_id="google-place-id",
            source="tiktok",
            generate_emoji=True,
            classify_photo=True,
            created_new=False,
            force_blend=True,
        )
        generate_mock = AsyncMock(return_value=[0.0])
        fake_supabase = _FakeSupabase(claimed=True)

        with (
            patch.object(
                location_tasks,
                "_get_location",
                return_value={
                    "location_id": 5987,
                    "updated_vibe": False,
                },
            ),
            patch.object(location_tasks, "generate_vibe_tags_for_location", generate_mock),
            patch.object(location_tasks, "get_supabase_service", return_value=fake_supabase),
        ):
            await location_tasks.vibe_reprocess_task(payload)

        generate_mock.assert_awaited_once_with(5987, num_runs=3, blend_tiktok=True)
        self.assertEqual(fake_supabase.claim_calls, [(5987, "request-5", 900)])
        self.assertEqual(fake_supabase.clear_calls, [(5987, "request-5")])

    async def test_vibe_reprocess_skips_when_lock_already_held(self) -> None:
        payload = VibeReprocessPayload(
            task_type="vibe_reprocess",
            request_id="request-6",
            location_id=5987,
            google_place_id="google-place-id",
            source="tiktok",
            generate_emoji=True,
            classify_photo=True,
            created_new=False,
            force_blend=True,
        )
        generate_mock = AsyncMock(return_value=[0.0])
        fake_supabase = _FakeSupabase(claimed=False)

        with (
            patch.object(
                location_tasks,
                "_get_location",
                return_value={
                    "location_id": 5987,
                    "updated_vibe": False,
                },
            ),
            patch.object(location_tasks, "generate_vibe_tags_for_location", generate_mock),
            patch.object(location_tasks, "get_supabase_service", return_value=fake_supabase),
        ):
            await location_tasks.vibe_reprocess_task(payload)

        generate_mock.assert_not_awaited()
        self.assertEqual(fake_supabase.claim_calls, [(5987, "request-6", 900)])
        self.assertEqual(fake_supabase.clear_calls, [])
