import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests import install_optional_dependency_stubs

install_optional_dependency_stubs()

from pinit.api.services.magic_ai_signature import (
    build_magic_ai_signature,
    geo_bucket,
    radius_bucket,
)
from pinit.api.services.magic_intent_parser import parse_magic_intent


class MagicAISignatureTests(unittest.TestCase):
    def test_geo_bucket_rounds_to_neighbourhood_scale(self) -> None:
        self.assertEqual(geo_bucket(51.49414, -0.17383), "51.494:-0.174")

    def test_radius_bucket_groups_close_values(self) -> None:
        self.assertEqual(radius_bucket(0.8), "1km")
        self.assertEqual(radius_bucket(1.6), "2km")
        self.assertEqual(radius_bucket(4.2), "5km")
        self.assertEqual(radius_bucket(8.0), "10km")
        self.assertEqual(radius_bucket(30.0), "50km")

    def test_similar_date_prompts_share_canonical_intent_hash(self) -> None:
        first = build_magic_ai_signature(
            intent=parse_magic_intent("cute date night spot near me"),
            raw_prompt="cute date night spot near me",
            lat=51.49414,
            lng=-0.17383,
            radius_km=2.0,
        )
        second = build_magic_ai_signature(
            intent=parse_magic_intent("romantic dinner nearby"),
            raw_prompt="romantic dinner nearby",
            lat=51.49418,
            lng=-0.17380,
            radius_km=2.1,
        )

        self.assertEqual(first["geo_bucket"], second["geo_bucket"])
        self.assertEqual(first["radius_bucket"], second["radius_bucket"])
        self.assertEqual(first["primary_intent"], "date_spot")
        self.assertEqual(second["primary_intent"], "date_spot")
        self.assertEqual(first["meal"], "dinner")
        self.assertEqual(second["meal"], "dinner")
        self.assertEqual(first["intent_hash"], second["intent_hash"])

    def test_freshness_detects_hot_new_and_viral_terms(self) -> None:
        signature = build_magic_ai_signature(
            intent=parse_magic_intent("hot new viral restaurants in South Kensington"),
            raw_prompt="hot new viral restaurants in South Kensington",
            lat=51.49414,
            lng=-0.17383,
            radius_km=2.0,
        )

        self.assertEqual(signature["freshness"], "viral")

        recent = build_magic_ai_signature(
            intent=parse_magic_intent("latest restaurants in South Kensington"),
            raw_prompt="latest restaurants in South Kensington",
            lat=51.49414,
            lng=-0.17383,
            radius_km=2.0,
        )
        new_opening = build_magic_ai_signature(
            intent=parse_magic_intent("new opening restaurants in South Kensington"),
            raw_prompt="new opening restaurants in South Kensington",
            lat=51.49414,
            lng=-0.17383,
            radius_km=2.0,
        )

        self.assertEqual(recent["freshness"], "recent")
        self.assertEqual(new_opening["freshness"], "new_opening")


if __name__ == "__main__":
    unittest.main()
