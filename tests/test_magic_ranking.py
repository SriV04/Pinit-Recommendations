import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests import install_optional_dependency_stubs

install_optional_dependency_stubs()

from pinit.api.services.magic_intent_parser import parse_magic_intent
from pinit.core.recommendation.magic_explanations import build_match_reasons
from pinit.core.recommendation.magic_ranking import (
    dedupe_magic_candidates,
    rerank_magic_candidates,
)


class MagicRankingTests(unittest.TestCase):
    def test_dedupe_magic_candidates_uses_place_id_location_id_then_name_proximity(self) -> None:
        candidates = [
            {
                "location_id": 10,
                "google_place_id": "place-a",
                "name": "Same Place",
                "lat": 51.50000,
                "lng": -0.12000,
            },
            {
                "location_id": 10,
                "google_place_id": "place-a",
                "name": "Same Place Duplicate",
                "lat": 51.50001,
                "lng": -0.12001,
            },
            {
                "location_id": -20,
                "google_place_id": "place-b",
                "name": "Close Name Match",
                "lat": 51.50000,
                "lng": -0.12000,
            },
            {
                "location_id": -21,
                "google_place_id": "place-c",
                "name": "Close Name Match",
                "lat": 51.50003,
                "lng": -0.12003,
            },
        ]

        deduped = dedupe_magic_candidates(candidates)

        self.assertEqual([item["google_place_id"] for item in deduped], ["place-a", "place-b"])

    def test_intent_reranker_boosts_matching_place_over_raw_base_score(self) -> None:
        intent = parse_magic_intent("cheap vibey Thai for a group tonight")
        candidates = [
            {
                "location_id": 1,
                "google_place_id": "thai-place",
                "name": "Thai Group Spot",
                "cuisine_primary": "thai",
                "types": "restaurant",
                "rating": 4.4,
                "user_ratings_total": 180,
                "price_level": 1,
                "open_now": True,
                "distance_km": 0.4,
                "final_score": 0.55,
                "quality_score": 0.72,
                "good_for_groups": True,
                "social_score": 0.0,
                "is_known_location": True,
            },
            {
                "location_id": 2,
                "google_place_id": "italian-place",
                "name": "Expensive Italian",
                "cuisine_primary": "italian",
                "types": "restaurant",
                "rating": 4.8,
                "user_ratings_total": 1200,
                "price_level": 4,
                "open_now": True,
                "distance_km": 0.2,
                "final_score": 0.92,
                "quality_score": 0.94,
                "good_for_groups": False,
                "social_score": 0.0,
                "is_known_location": True,
            },
        ]

        ranked = rerank_magic_candidates(
            candidates,
            intent=intent,
            request_radius_km=2.0,
        )

        self.assertEqual(ranked[0]["google_place_id"], "thai-place")
        self.assertGreater(ranked[0]["intent_matches"]["overall"], 0.75)
        self.assertGreater(ranked[0]["final_score"], ranked[1]["final_score"])

    def test_match_reasons_are_deterministic_from_score_components(self) -> None:
        intent = parse_magic_intent("cheap vibey Thai for a group tonight")
        candidate = {
            "name": "Thai Group Spot",
            "cuisine_primary": "thai",
            "price_level": 1,
            "good_for_groups": True,
            "open_now": True,
            "social_score": 0.3,
            "vibe_score": 0.8,
            "intent_matches": {
                "budget": 1.0,
                "cuisine": 1.0,
                "occasion": 1.0,
                "availability": 1.0,
            },
        }

        reasons = build_match_reasons(candidate, intent, candidate["intent_matches"])

        self.assertIn("Matches cheap Thai", reasons)
        self.assertIn("Good for groups", reasons)
        self.assertIn("Open now", reasons)
        self.assertIn("People you trust have saved this", reasons)

    def test_web_agent_score_is_capped_and_source_metadata_is_preserved(self) -> None:
        intent = parse_magic_intent("hot date night in Soho")
        ranked = rerank_magic_candidates(
            [
                {
                    "location_id": -1,
                    "google_place_id": "gid-1",
                    "name": "Test",
                    "lat": 51.51,
                    "lng": -0.13,
                    "distance_km": 0.3,
                    "rating": 4.5,
                    "user_ratings_total": 200,
                    "final_score": 0.4,
                    "web_agent": {
                        "reason": "Recent date-night list mention",
                        "confidence": 1.0,
                        "source_claims": ["ai_hotspot", "date_spot"],
                        "citations": [{"url": "https://example.com", "title": "List"}],
                    },
                }
            ],
            intent=intent,
            request_radius_km=2.0,
        )

        agentic_web = ranked[0]["intent_matches"]["agentic_web"]
        self.assertLessEqual(agentic_web, 0.20)
        self.assertGreaterEqual(agentic_web, 0.15)  # confident agentic finds get a strong boost
        self.assertIn("AI Agent", ranked[0]["source"])
        self.assertEqual(ranked[0]["source_metadata"][0]["source"], "ai_agent")
        self.assertIn("Suggested by Magic Search AI", ranked[0]["match_reasons"])


    def test_confident_ai_only_candidate_outranks_mediocre_known_venue(self) -> None:
        intent = parse_magic_intent("hot new restaurants in brighton")
        # Lean AI-only candidate, shaped like _magic_ai_enrichment_candidate():
        # no Google rating, but base + quality seeded from agent confidence.
        ai_only = {
            "location_id": -99,
            "google_place_id": "gid-ai",
            "name": "The Set",
            "lat": 50.83,
            "lng": -0.14,
            "distance_km": 0.5,
            "quality_score": 0.9,
            "final_score": 0.9,
            "web_agent": {
                "reason": "Buzzy new opening",
                "confidence": 0.9,
                "source_claims": ["new_opening", "critic_mentioned"],
                "citations": [],
            },
        }
        known = {
            "location_id": 1,
            "google_place_id": "gid-known",
            "name": "Average Diner",
            "lat": 50.83,
            "lng": -0.14,
            "distance_km": 0.5,
            "rating": 3.9,
            "user_ratings_total": 80,
            "final_score": 0.45,
        }
        ranked = rerank_magic_candidates(
            [known, ai_only], intent=intent, request_radius_km=2.0
        )
        self.assertEqual(ranked[0]["name"], "The Set")
        self.assertIn("AI Agent", ranked[0]["source"])


if __name__ == "__main__":
    unittest.main()
