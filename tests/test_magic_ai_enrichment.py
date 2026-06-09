import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests import install_optional_dependency_stubs

install_optional_dependency_stubs()

from pinit.api.services.magic_ai_enrichment import build_magic_ai_enrichment_payload


class _FakeSupabase:
    vibe_tag_order = {}

    def get_locations_by_google_place_ids(self, ids):
        if "gid-1" in ids:
            return {"gid-1": {"location_id": 123}}
        return {}


class MagicAIEnrichmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_payload_is_flutter_ready_and_matched_to_supabase(self) -> None:
        signature = {
            "geo_bucket": "51.494:-0.174",
            "radius_bucket": "2km",
            "intent_hash": "abc123",
            "freshness": "recent",
            "primary_intent": "date_spot",
            "meal": "dinner",
            "cuisines": [],
            "vibes": [],
            "venue_types": ["restaurant"],
            "budget": None,
            "canonical_text": "date_spot dinner restaurant recent",
        }
        xai_result = [
            {
                "name": "Test Place",
                "google_place_id": "ignored-xai-id",
                "place_resolution_query": "Test Place South Kensington London",
                "address_hint": "South Kensington",
                "neighbourhood_hint": "South Kensington",
                "reason": "Recent mention",
                "confidence": 0.8,
                "source_claims": ["ai_hotspot"],
                "citations": [{"url": "https://example.com", "title": "List"}],
            },
            {
                "name": "Google Only Place",
                "google_place_id": "ignored-google-only-id",
                "place_resolution_query": "Google Only Place London",
                "reason": "Google resolved but not in Supabase",
                "confidence": 0.7,
                "source_claims": ["critic_mentioned"],
                "citations": [],
            },
            {
                "name": "Unresolved Place",
                "google_place_id": "ignored-unresolved-id",
                "place_resolution_query": "Unresolved Place London",
                "reason": "No Google match",
                "confidence": 0.4,
                "source_claims": ["social_buzz"],
                "citations": [],
            },
        ]
        google_places = [
            {
                "id": "gid-1",
                "displayName": {"text": "Test Place"},
                "formattedAddress": "South Kensington, London",
                "location": {"latitude": 51.494, "longitude": -0.174},
                "web_agent": xai_result[0],
            },
            {
                "id": "gid-2",
                "displayName": {"text": "Google Only Place"},
                "formattedAddress": "Chelsea, London",
                "location": {"latitude": 51.49, "longitude": -0.18},
                "web_agent": xai_result[1],
            },
        ]
        fetch_suggestions = AsyncMock(return_value=xai_result)
        resolve_suggestions = AsyncMock(return_value=google_places)

        payload = await build_magic_ai_enrichment_payload(
            prompt="hot date spot",
            lat=51.4941,
            lng=-0.1738,
            radius_km=2.0,
            signature=signature,
            user_profile={"vibe_vector": []},
            supabase=_FakeSupabase(),
            fetch_suggestions=fetch_suggestions,
            resolve_suggestions=resolve_suggestions,
        )

        self.assertEqual(payload["source"], "xai_web_search")
        self.assertEqual(payload["canonical_signature"], signature)
        self.assertIn("generated_at", payload)
        self.assertIn("expires_at", payload)
        self.assertEqual(payload["items"][0]["google_place_id"], "gid-1")
        self.assertEqual(payload["items"][0]["name"], "Test Place")
        self.assertEqual(
            payload["items"][0]["place_resolution_query"],
            "Test Place South Kensington London",
        )
        self.assertEqual(payload["items"][0]["address_hint"], "South Kensington")
        self.assertEqual(payload["items"][0]["neighbourhood_hint"], "South Kensington")
        self.assertEqual(
            payload["items"][0]["formatted_address"],
            "South Kensington, London",
        )
        self.assertEqual(payload["items"][0]["lat"], 51.494)
        self.assertEqual(payload["items"][0]["lng"], -0.174)
        self.assertEqual(payload["items"][0]["reason"], "Recent mention")
        self.assertEqual(payload["items"][0]["confidence"], 0.8)
        self.assertEqual(payload["items"][0]["source_claims"], ["ai_hotspot"])
        self.assertEqual(
            payload["items"][0]["citations"],
            [{"url": "https://example.com", "title": "List"}],
        )
        self.assertEqual(payload["items"][0]["matched_location_id"], 123)
        self.assertEqual(payload["items"][0]["match_status"], "supabase_hit")
        self.assertEqual(payload["items"][1]["google_place_id"], "gid-2")
        self.assertIsNone(payload["items"][1]["matched_location_id"])
        self.assertEqual(payload["items"][1]["match_status"], "google_resolved")
        self.assertIsNone(payload["items"][2]["google_place_id"])
        self.assertIsNone(payload["items"][2]["matched_location_id"])
        self.assertEqual(payload["items"][2]["match_status"], "unresolved")
        resolve_suggestions.assert_awaited_once()
        resolver_suggestions = resolve_suggestions.await_args.args[0]
        self.assertTrue(
            all("google_place_id" not in item for item in resolver_suggestions)
        )


if __name__ == "__main__":
    unittest.main()
