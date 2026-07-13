from __future__ import annotations

from pathlib import Path
import sys
from unittest.mock import MagicMock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from pinit.api.services.proximal_service import (
    refresh_location_from_google_place_details,
    resolve_google_place_id_for_location,
)


class _Response:
    def __init__(self, body: dict, *, ok: bool = True) -> None:
        self._body = body
        self.ok = ok
        self.status_code = 200 if ok else 500
        self.text = ""

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if not self.ok:
            raise RuntimeError("request failed")


def _full_place_payload() -> dict:
    return {
        "id": "place-42",
        "displayName": {"text": "Noodle Yard"},
        "types": ["restaurant", "japanese_restaurant"],
        "shortFormattedAddress": "42 Test Street, London",
        "formattedAddress": "42 Test Street, London, UK",
        "location": {"latitude": 51.5, "longitude": -0.1},
        "businessStatus": "OPERATIONAL",
        "googleMapsUri": "https://maps.google.com/?cid=42",
        "rating": 0,
        "userRatingCount": 0,
        "priceLevel": "PRICE_LEVEL_FREE",
        "goodForChildren": False,
        "reviews": [
            {
                "authorAttribution": {"displayName": "A Reviewer"},
                "text": {"text": "Excellent noodles", "languageCode": "en"},
                "rating": 5,
                "publishTime": "2026-07-01T12:00:00Z",
            }
        ],
    }


@patch("pinit.api.services.proximal_service.GOOGLE_PLACE_API_KEY", "test-key")
@patch("pinit.api.services.proximal_service.get_supabase_service")
@patch("pinit.api.services.proximal_service.requests.get")
def test_full_refresh_requests_reviews_and_marks_success(
    fake_get: MagicMock,
    get_supabase: MagicMock,
) -> None:
    fake_get.return_value = _Response(_full_place_payload())
    supabase = get_supabase.return_value
    supabase.update_location.return_value = {"location_id": 42}

    result = refresh_location_from_google_place_details(42, "place-42")

    assert result is not None
    field_mask = fake_get.call_args.kwargs["headers"]["X-Goog-FieldMask"]
    assert "reviews" in field_mask.split(",")
    update = supabase.update_location.call_args.kwargs
    assert update["google_details_fetched_at"].endswith("+00:00")
    assert update["reviews"]


@patch("pinit.api.services.proximal_service.GOOGLE_PLACE_API_KEY", "test-key")
@patch("pinit.api.services.proximal_service.get_supabase_service")
@patch("pinit.api.services.proximal_service.requests.get")
def test_omitted_optional_values_do_not_erase_existing_data(
    fake_get: MagicMock,
    get_supabase: MagicMock,
) -> None:
    payload = _full_place_payload()
    payload.pop("reviews")
    fake_get.return_value = _Response(payload)
    supabase = get_supabase.return_value
    supabase.update_location.return_value = {"location_id": 42}

    refresh_location_from_google_place_details(42, "place-42")

    update = supabase.update_location.call_args.kwargs
    assert "website" not in update
    assert "reviews" not in update
    assert "opening_hours_text" not in update
    assert update["good_for_children"] is False
    assert update["price_level"] == 0


@patch("pinit.api.services.proximal_service.requests.post")
def test_existing_place_id_skips_text_search(fake_post: MagicMock) -> None:
    place_id = resolve_google_place_id_for_location(
        {
            "google_place_id": " place-42 ",
            "name": "Noodle Yard",
            "lat": 51.5,
            "lng": -0.1,
        }
    )

    assert place_id == "place-42"
    fake_post.assert_not_called()


@patch("pinit.api.services.proximal_service.GOOGLE_PLACE_API_KEY", "test-key")
@patch("pinit.api.services.proximal_service.requests.post")
def test_missing_place_id_accepts_one_exact_nearby_match(fake_post: MagicMock) -> None:
    fake_post.return_value = _Response(
        {
            "places": [
                {
                    "id": "resolved-42",
                    "displayName": {"text": "Noodle Yard"},
                    "formattedAddress": "42 Test Street, London, UK",
                    "location": {"latitude": 51.5002, "longitude": -0.1001},
                }
            ]
        }
    )

    place_id = resolve_google_place_id_for_location(
        {
            "google_place_id": None,
            "name": "Noodle Yard",
            "vicinity": "42 Test Street, London",
            "lat": 51.5,
            "lng": -0.1,
        }
    )

    assert place_id == "resolved-42"
    assert fake_post.call_count == 1


@patch("pinit.api.services.proximal_service.GOOGLE_PLACE_API_KEY", "test-key")
@patch("pinit.api.services.proximal_service.requests.post")
def test_missing_place_id_rejects_ambiguous_matches(fake_post: MagicMock) -> None:
    fake_post.return_value = _Response(
        {
            "places": [
                {
                    "id": "resolved-1",
                    "displayName": {"text": "Noodle Yard"},
                    "location": {"latitude": 51.5001, "longitude": -0.1001},
                },
                {
                    "id": "resolved-2",
                    "displayName": {"text": "Noodle Yard"},
                    "location": {"latitude": 51.5002, "longitude": -0.1002},
                },
            ]
        }
    )

    place_id = resolve_google_place_id_for_location(
        {
            "google_place_id": None,
            "name": "Noodle Yard",
            "lat": 51.5,
            "lng": -0.1,
        }
    )

    assert place_id is None
