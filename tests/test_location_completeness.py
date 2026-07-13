from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from pinit.api.services.location_completeness import (
    GOOGLE_REFRESH_AFTER,
    build_location_processing_plan,
)


NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def _complete_row() -> dict:
    return {
        "google_details_fetched_at": (NOW - timedelta(days=1)).isoformat(),
        "website": None,
        "reviews": None,
        "rating": 0,
        "good_for_children": False,
        "image_stored": True,
        "image_unavailable": False,
        "emoji": "🍜",
        "generated_summary": "A compact neighbourhood noodle bar.",
        "cuisine_primary": "japanese",
        "dietary_requirement_vector": [0, 20, 80],
        "vibe_vector": [0.1, 0.8],
        "updated_vibe": True,
    }


def test_missing_google_fetch_timestamp_is_due() -> None:
    row = _complete_row()
    row["google_details_fetched_at"] = None

    plan = build_location_processing_plan(row, now=NOW)

    assert plan.google_due is True
    assert "google:not_fetched" in plan.reasons


def test_google_fetch_at_exactly_thirty_days_is_due() -> None:
    row = _complete_row()
    row["google_details_fetched_at"] = (
        NOW - GOOGLE_REFRESH_AFTER
    ).isoformat()

    plan = build_location_processing_plan(row, now=NOW)

    assert plan.google_due is True
    assert "google:stale" in plan.reasons


def test_fresh_google_fetch_accepts_optional_null_false_and_zero_values() -> None:
    plan = build_location_processing_plan(_complete_row(), now=NOW)

    assert plan.google_due is False
    assert plan.is_complete is True


def test_photo_is_complete_when_google_has_no_photo() -> None:
    row = _complete_row()
    row["image_stored"] = False
    row["image_unavailable"] = True

    plan = build_location_processing_plan(row, now=NOW)

    assert plan.photo_due is False


def test_photo_is_due_without_either_terminal_flag() -> None:
    row = _complete_row()
    row["image_stored"] = False
    row["image_unavailable"] = False

    plan = build_location_processing_plan(row, now=NOW)

    assert plan.photo_due is True
    assert "photo:missing" in plan.reasons


def test_missing_content_uses_menu_then_fallback_when_website_exists() -> None:
    row = _complete_row()
    row["website"] = "https://example.com"
    row["generated_summary"] = None

    plan = build_location_processing_plan(row, now=NOW)

    assert plan.menu_due is True
    assert plan.content_fallback_due is True
    assert "content:generated_summary" in plan.reasons


def test_missing_content_without_website_skips_menu_but_requires_fallback() -> None:
    row = _complete_row()
    row["generated_summary"] = None

    plan = build_location_processing_plan(row, now=NOW)

    assert plan.menu_due is False
    assert plan.content_fallback_due is True


def test_vibe_requires_vector_and_completion_flag() -> None:
    row = _complete_row()
    row["updated_vibe"] = True
    row["vibe_vector"] = []

    missing_vector = build_location_processing_plan(row, now=NOW)

    row["vibe_vector"] = [0.1, 0.2]
    row["updated_vibe"] = False
    missing_flag = build_location_processing_plan(row, now=NOW)

    assert missing_vector.vibe_due is True
    assert missing_flag.vibe_due is True
    assert "vibe:missing" in missing_vector.reasons
    assert "vibe:missing" in missing_flag.reasons


def test_blank_emoji_is_due() -> None:
    row = _complete_row()
    row["emoji"] = "  "

    plan = build_location_processing_plan(row, now=NOW)

    assert plan.emoji_due is True
    assert "emoji:missing" in plan.reasons
