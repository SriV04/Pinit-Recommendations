from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from pinit.api.services.location_content_fallback import (
    LocationContentFallback,
    fill_missing_location_content,
)


@patch("pinit.api.services.location_content_fallback.get_supabase_service")
def test_fallback_fills_only_missing_content(get_supabase: MagicMock) -> None:
    row = {
        "location_id": 42,
        "generated_summary": None,
        "cuisine_primary": "thai",
        "dietary_requirement_vector": None,
        "reviews": [{"rating": 5, "text": "Great vegan curries"}],
    }
    result = LocationContentFallback(
        summary="A lively neighbourhood Thai restaurant.",
        cuisine_primary="thai",
        dietary_requirement_vector=[40, 80, 20, 90, 30, 10],
    )
    analyzer = AsyncMock(return_value=result)
    supabase = get_supabase.return_value
    supabase.update_location.return_value = {"location_id": 42}

    update = asyncio.run(fill_missing_location_content(row, analyzer=analyzer))

    analyzer.assert_awaited_once_with(row)
    assert update["generated_summary"] == result.summary
    assert update["dietary_requirement_vector"] == result.dietary_requirement_vector
    assert "cuisine_primary" not in update
    assert "reccomended_dishes" not in update
    assert update["menu_analysis_confidence"] == "google_fallback"
    supabase.update_location.assert_called_once_with(42, **update)


@patch("pinit.api.services.location_content_fallback.get_supabase_service")
def test_complete_content_skips_analyzer_and_write(get_supabase: MagicMock) -> None:
    row = {
        "location_id": 42,
        "generated_summary": "Already populated.",
        "cuisine_primary": "thai",
        "dietary_requirement_vector": [10, 20, 30, 40, 50, 60],
    }
    analyzer = AsyncMock()

    update = asyncio.run(fill_missing_location_content(row, analyzer=analyzer))

    assert update == {}
    analyzer.assert_not_awaited()
    get_supabase.assert_not_called()


def test_invalid_fallback_analysis_is_retryable() -> None:
    row = {
        "location_id": 42,
        "generated_summary": None,
        "cuisine_primary": None,
        "dietary_requirement_vector": None,
    }
    analyzer = AsyncMock(return_value={})

    with pytest.raises(ValidationError):
        asyncio.run(fill_missing_location_content(row, analyzer=analyzer))
