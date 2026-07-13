from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from tests import install_optional_dependency_stubs
except Exception:  # pragma: no cover
    install_optional_dependency_stubs = None

if install_optional_dependency_stubs is not None:
    install_optional_dependency_stubs()

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from pinit.api.schemas_location_tasks import ProcessLocationPayload
from pinit.api.services import location_tasks


FRESH = datetime.now(timezone.utc).isoformat()


def _row(**overrides):
    row = {
        "location_id": 42,
        "google_place_id": "place-42",
        "google_details_fetched_at": FRESH,
        "name": "Test Place",
        "website": "https://test.example",
        "photos": [{"name": "places/place-42/photos/photo-1"}],
        "image_stored": True,
        "image_unavailable": False,
        "emoji": "🍜",
        "generated_summary": "A useful summary.",
        "cuisine_primary": "thai",
        "dietary_requirement_vector": [10, 20, 30, 40, 50, 60],
        "vibe_vector": [0.1, 0.2],
        "updated_vibe": True,
    }
    row.update(overrides)
    return row


def _payload(**overrides):
    values = {
        "task_type": "process_location",
        "request_id": "request-1",
        "location_id": 42,
        "google_place_id": "place-42",
        "source": "expanded-card-open",
    }
    values.update(overrides)
    return ProcessLocationPayload(**values)


class ProcessLocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_row_makes_no_external_stage_calls(self) -> None:
        with (
            patch.object(location_tasks, "_get_location", new=AsyncMock(return_value=_row())),
            patch.object(location_tasks, "refresh_location_from_google_place_details") as google,
            patch.object(location_tasks, "photos_task", new=AsyncMock()) as photos,
            patch.object(location_tasks, "emoji_task", new=AsyncMock()) as emoji,
            patch.object(location_tasks, "process_menu_for_location", new=AsyncMock()) as menu,
            patch.object(location_tasks, "fill_missing_location_content", new=AsyncMock()) as fallback,
            patch.object(location_tasks, "generate_vibe_tags_for_location", new=AsyncMock()) as vibe,
        ):
            await location_tasks.process_location_task(
                _payload(generate_emoji=False, classify_photo=False)
            )

        google.assert_not_called()
        photos.assert_not_awaited()
        emoji.assert_not_awaited()
        menu.assert_not_awaited()
        fallback.assert_not_awaited()
        vibe.assert_not_awaited()

    async def test_runs_each_missing_stage_and_replans_between_dependencies(self) -> None:
        initial = _row(
            google_details_fetched_at=None,
            image_stored=False,
            emoji=None,
            generated_summary=None,
            cuisine_primary=None,
            dietary_requirement_vector=None,
            vibe_vector=None,
            updated_vibe=False,
        )
        after_google = _row(
            image_stored=False,
            emoji=None,
            generated_summary=None,
            cuisine_primary=None,
            dietary_requirement_vector=None,
            vibe_vector=None,
            updated_vibe=False,
        )
        after_basic = _row(
            generated_summary=None,
            cuisine_primary=None,
            dietary_requirement_vector=None,
            vibe_vector=None,
            updated_vibe=False,
        )
        after_menu = _row(
            generated_summary=None,
            cuisine_primary=None,
            dietary_requirement_vector=None,
            vibe_vector=None,
            updated_vibe=False,
        )
        after_fallback = _row(vibe_vector=None, updated_vibe=False)
        complete = _row()
        get_row = AsyncMock(
            side_effect=[initial, after_google, after_basic, after_menu, after_fallback, complete]
        )

        with (
            patch.object(location_tasks, "_get_location", new=get_row),
            patch.object(
                location_tasks,
                "refresh_location_from_google_place_details",
                return_value={"location_id": 42},
            ) as google,
            patch.object(location_tasks, "photos_task", new=AsyncMock()) as photos,
            patch.object(location_tasks, "emoji_task", new=AsyncMock()) as emoji,
            patch.object(location_tasks, "process_menu_for_location", new=AsyncMock()) as menu,
            patch.object(location_tasks, "fill_missing_location_content", new=AsyncMock()) as fallback,
            patch.object(
                location_tasks,
                "_generate_vibe_for_process_location",
                new=AsyncMock(),
            ) as vibe,
        ):
            await location_tasks.process_location_task(
                _payload(generate_emoji=False, classify_photo=False)
            )

        google.assert_called_once_with(42, "place-42")
        photos.assert_awaited_once()
        emoji.assert_awaited_once()
        menu.assert_awaited_once()
        fallback.assert_awaited_once_with(after_menu)
        vibe.assert_awaited_once()
        self.assertEqual(get_row.await_count, 6)

    async def test_google_failure_stops_downstream_work(self) -> None:
        initial = _row(google_details_fetched_at=None, image_stored=False, emoji=None)
        with (
            patch.object(location_tasks, "_get_location", new=AsyncMock(return_value=initial)),
            patch.object(
                location_tasks,
                "refresh_location_from_google_place_details",
                return_value=None,
            ),
            patch.object(location_tasks, "photos_task", new=AsyncMock()) as photos,
            patch.object(location_tasks, "emoji_task", new=AsyncMock()) as emoji,
        ):
            with self.assertRaises(RuntimeError):
                await location_tasks.process_location_task(_payload())

        photos.assert_not_awaited()
        emoji.assert_not_awaited()

    async def test_missing_place_id_is_resolved_once_and_persisted(self) -> None:
        initial = _row(
            google_place_id=None,
            google_details_fetched_at=None,
        )
        complete = _row()
        supabase = MagicMock()
        supabase.update_location.return_value = {"location_id": 42}

        with (
            patch.object(location_tasks, "_get_location", new=AsyncMock(side_effect=[initial, complete])),
            patch.object(location_tasks, "resolve_google_place_id_for_location", return_value="resolved-42") as resolve,
            patch.object(
                location_tasks,
                "refresh_location_from_google_place_details",
                return_value={"location_id": 42},
            ) as refresh,
            patch.object(location_tasks, "get_supabase_service", return_value=supabase),
        ):
            await location_tasks.process_location_task(_payload(google_place_id=""))

        resolve.assert_called_once_with(initial)
        supabase.update_location.assert_called_once_with(42, google_place_id="resolved-42")
        refresh.assert_called_once_with(42, "resolved-42")

    async def test_no_website_goes_directly_to_google_fallback(self) -> None:
        missing = _row(
            website=None,
            generated_summary=None,
            cuisine_primary=None,
            dietary_requirement_vector=None,
        )
        complete = _row(website=None)

        with (
            patch.object(location_tasks, "_get_location", new=AsyncMock(side_effect=[missing, complete])),
            patch.object(location_tasks, "process_menu_for_location", new=AsyncMock()) as menu,
            patch.object(location_tasks, "fill_missing_location_content", new=AsyncMock()) as fallback,
        ):
            await location_tasks.process_location_task(_payload())

        menu.assert_not_awaited()
        fallback.assert_awaited_once_with(missing)

    async def test_retry_skips_stages_already_persisted(self) -> None:
        partial = _row(
            generated_summary=None,
            cuisine_primary=None,
            dietary_requirement_vector=None,
            vibe_vector=None,
            updated_vibe=False,
        )
        after_menu = _row(vibe_vector=None, updated_vibe=False)
        complete = _row()

        with (
            patch.object(
                location_tasks,
                "_get_location",
                new=AsyncMock(side_effect=[partial, after_menu, complete]),
            ),
            patch.object(location_tasks, "photos_task", new=AsyncMock()) as photos,
            patch.object(location_tasks, "emoji_task", new=AsyncMock()) as emoji,
            patch.object(location_tasks, "process_menu_for_location", new=AsyncMock()) as menu,
            patch.object(location_tasks, "fill_missing_location_content", new=AsyncMock()) as fallback,
            patch.object(
                location_tasks,
                "_generate_vibe_for_process_location",
                new=AsyncMock(),
            ) as vibe,
        ):
            await location_tasks.process_location_task(_payload())

        photos.assert_not_awaited()
        emoji.assert_not_awaited()
        menu.assert_awaited_once()
        fallback.assert_not_awaited()
        vibe.assert_awaited_once()
