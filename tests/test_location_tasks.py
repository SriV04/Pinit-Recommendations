from __future__ import annotations

import unittest
import sys
from pathlib import Path
from unittest.mock import patch

try:
    from tests import install_optional_dependency_stubs
except Exception:  # pragma: no cover
    install_optional_dependency_stubs = None

if install_optional_dependency_stubs is not None:
    install_optional_dependency_stubs()

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from pinit.api.schemas_location_tasks import PipelinePayload
from pinit.api.services import location_tasks


class _FakeDispatcher:
    def __init__(self) -> None:
        self.dispatched = []

    async def dispatch(self, payload) -> None:
        self.dispatched.append(payload)


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
