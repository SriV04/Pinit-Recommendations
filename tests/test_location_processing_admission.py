import unittest
import sys
from pathlib import Path
from unittest.mock import AsyncMock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from pinit.api.schemas_location_tasks import ProcessLocationPayload
from pinit.api.services.location_processing_admission import (
    admit_location_processing,
)


def _payload() -> ProcessLocationPayload:
    return ProcessLocationPayload(
        task_type="process_location",
        request_id="request-42",
        location_id=42,
        google_place_id="place-42",
        source="expanded-card-open",
    )


class _FakeSupabase:
    def __init__(self, *, claimed: bool = True) -> None:
        self.claimed = claimed
        self.claim_calls: list[tuple] = []
        self.complete_calls: list[tuple] = []
        self.release_calls: list[tuple] = []

    def claim_location_processing(
        self,
        location_id,
        request_id,
        **kwargs,
    ):
        self.claim_calls.append((location_id, request_id, kwargs))
        return self.claimed

    def complete_location_processing_queue(self, location_id, request_id):
        self.complete_calls.append((location_id, request_id))
        return True

    def release_location_processing_claim(self, location_id, request_id):
        self.release_calls.append((location_id, request_id))
        return True


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_denied_claim_does_not_dispatch(self) -> None:
        supabase = _FakeSupabase(claimed=False)
        dispatch = AsyncMock()

        result = await admit_location_processing(
            _payload(),
            supabase=supabase,
            dispatch=dispatch,
        )

        self.assertFalse(result.queued)
        dispatch.assert_not_awaited()
        self.assertEqual(supabase.complete_calls, [])
        self.assertEqual(supabase.release_calls, [])

    async def test_successful_dispatch_completes_tracker(self) -> None:
        supabase = _FakeSupabase()
        dispatch = AsyncMock()
        payload = _payload()

        result = await admit_location_processing(
            payload,
            supabase=supabase,
            dispatch=dispatch,
        )

        self.assertTrue(result.queued)
        dispatch.assert_awaited_once_with(payload)
        self.assertEqual(supabase.complete_calls, [(42, "request-42")])
        self.assertEqual(supabase.release_calls, [])

    async def test_failed_dispatch_releases_claim(self) -> None:
        supabase = _FakeSupabase()
        dispatch = AsyncMock(side_effect=RuntimeError("publish failed"))

        with self.assertRaisesRegex(RuntimeError, "publish failed"):
            await admit_location_processing(
                _payload(),
                supabase=supabase,
                dispatch=dispatch,
            )

        self.assertEqual(supabase.complete_calls, [])
        self.assertEqual(supabase.release_calls, [(42, "request-42")])
