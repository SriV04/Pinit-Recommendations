from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    from tests import install_optional_dependency_stubs
except Exception:  # pragma: no cover
    install_optional_dependency_stubs = None

if install_optional_dependency_stubs is not None:
    install_optional_dependency_stubs()

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

_spec = importlib.util.spec_from_file_location(
    "migrate_photos_to_r2", REPO_ROOT / "scripts" / "migrate_photos_to_r2.py"
)
migrate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = migrate  # @dataclass needs the module registered
_spec.loader.exec_module(migrate)


def _obj(name, size=1000, mimetype="image/jpeg"):
    return {"name": name, "id": "x", "metadata": {"size": size, "mimetype": mimetype}}


def _all_keys(location_id, index, ext=".jpg"):
    return {
        f"l/{location_id}/{index}_thumb.webp",
        f"l/{location_id}/{index}_card.webp",
        f"l/{location_id}/{index}_hero.webp",
        f"l/{location_id}/{index}_orig{ext}",
    }


class ParseNameTests(unittest.TestCase):
    def test_primary_and_extras(self):
        self.assertEqual(migrate.parse_location_object_name("123.jpg"), (123, 0))
        self.assertEqual(migrate.parse_location_object_name("123_4.png"), (123, 4))
        self.assertEqual(migrate.parse_location_object_name("7_10.webp"), (7, 10))

    def test_unrecognised_names_are_rejected(self):
        for name in ("abc.jpg", "123", "123_.jpg", "folder/123.jpg", "123.jpg.bak.", ".emptyFolderPlaceholder"):
            self.assertIsNone(migrate.parse_location_object_name(name), name)


class PlanLocationsTests(unittest.TestCase):
    def test_fully_migrated_photos_are_skipped(self):
        plan = migrate.plan_locations([_obj("1.jpg"), _obj("2.jpg")], _all_keys(1, 0))

        self.assertEqual(plan.already_done, 1)
        self.assertEqual([i["location_id"] for i in plan.todo], [2])

    def test_partial_upload_is_redone(self):
        partial = _all_keys(1, 0) - {"l/1/0_hero.webp"}
        plan = migrate.plan_locations([_obj("1.jpg")], partial)

        self.assertEqual(plan.already_done, 0)
        self.assertEqual(len(plan.todo), 1)

    def test_missing_original_counts_as_not_done(self):
        no_original = {k for k in _all_keys(1, 0) if "_orig" not in k}
        plan = migrate.plan_locations([_obj("1.jpg")], no_original)

        self.assertEqual(len(plan.todo), 1)

    def test_original_extension_may_differ_from_source(self):
        plan = migrate.plan_locations([_obj("1.png", mimetype="image/png")], _all_keys(1, 0, ".png"))

        self.assertEqual(plan.already_done, 1)

    def test_extras_are_planned_independently(self):
        plan = migrate.plan_locations([_obj("5.jpg"), _obj("5_2.jpg")], _all_keys(5, 0))

        self.assertEqual([(i["location_id"], i["index"]) for i in plan.todo], [(5, 2)])

    def test_only_ids_filters_primaries_and_extras(self):
        objects = [_obj("1.jpg"), _obj("1_2.jpg"), _obj("2.jpg"), _obj("3_1.jpg")]
        plan = migrate.plan_locations(objects, set(), only_ids={1, 3})

        self.assertEqual(sorted((i["location_id"], i["index"]) for i in plan.todo), [(1, 0), (1, 2), (3, 1)])

    def test_empty_only_ids_plans_nothing(self):
        plan = migrate.plan_locations([_obj("1.jpg")], set(), only_ids=set())

        self.assertEqual(plan.todo, [])

    def test_unparseable_names_are_reported_not_planned(self):
        plan = migrate.plan_locations([_obj("weird-name.jpg"), _obj("9.jpg", size=500)], set())

        self.assertEqual(plan.unparseable, ["weird-name.jpg"])
        self.assertEqual(len(plan.todo), 1)
        self.assertEqual(plan.todo_bytes, 500)


class PlanCopiesTests(unittest.TestCase):
    def test_prefix_and_skip_existing(self):
        plan = migrate.plan_copies(
            [_obj("u1/a.jpg"), _obj("u2/b.jpg")], "u/", {"u/u1/a.jpg"}
        )

        self.assertEqual(plan.already_done, 1)
        self.assertEqual([i["key"] for i in plan.todo], ["u/u2/b.jpg"])


class EstimateCostTests(unittest.TestCase):
    def test_full_backfill_stays_inside_the_free_request_tier(self):
        est = migrate.estimate_cost(
            location_photos=31_386, copy_objects=399, source_bytes=int(9.5 * 1024**3), list_pages=150
        )

        self.assertEqual(est["class_a_requests"], 31_386 * 4 + 399 + 150)
        self.assertEqual(est["class_a_cost_usd"], 0.0)
        self.assertAlmostEqual(est["stored_gb"], 14.2, delta=0.4)
        self.assertAlmostEqual(est["storage_usd_month"], 0.06, delta=0.03)

    def test_requests_over_the_free_tier_are_billed(self):
        est = migrate.estimate_cost(location_photos=500_000, copy_objects=0, source_bytes=0)

        self.assertEqual(est["class_a_requests"], 2_000_000)
        self.assertAlmostEqual(est["class_a_cost_usd"], 4.50)


class MigrateLocationTests(unittest.TestCase):
    def test_uses_storage_mimetype_and_index(self):
        item = {"name": "42_3.png", "location_id": 42, "index": 3,
                "metadata": {"mimetype": "image/png"}}
        with (
            patch.object(migrate, "download", return_value=(b"bytes", "application/octet-stream")),
            patch.object(migrate.r2_photos, "upload_location_photo") as upload,
        ):
            moved = migrate.migrate_location(item)

        upload.assert_called_once_with(42, b"bytes", "image/png", 3)
        self.assertEqual(moved, 5)


class RunPoolTests(unittest.TestCase):
    def test_a_failure_is_recorded_and_the_rest_continue(self):
        def fn(item):
            if item["name"] == "bad":
                raise RuntimeError("boom")
            return 10

        failures = []
        moved = migrate.run_pool("t", [{"name": "ok1"}, {"name": "bad"}, {"name": "ok2"}], fn, 2, failures)

        self.assertEqual(moved, 20)
        self.assertEqual([f["name"] for f in failures], ["bad"])


class ReferencedIdsTests(unittest.TestCase):
    def test_pages_through_user_location_actions_and_dedupes(self):
        pages = [
            [{"location_id": i % 5} for i in range(1000)],  # full page -> keep going
            [{"location_id": 99}, {"location_id": None}],  # short page -> stop
        ]
        client = MagicMock()
        query = client.table.return_value.select.return_value.order.return_value.range.return_value
        query.execute.side_effect = [MagicMock(data=p) for p in pages]

        ids = migrate.fetch_referenced_location_ids(client)

        self.assertEqual(ids, {0, 1, 2, 3, 4, 99})
        client.table.assert_called_with("user_location_actions")
        client.table.return_value.select.return_value.order.assert_called_with("action_id")


class MainGuardTests(unittest.TestCase):
    def test_refuses_to_run_without_r2_config(self):
        with patch.object(migrate.r2_photos, "is_configured", return_value=False):
            self.assertEqual(migrate.main(["--dry-run"]), 2)


if __name__ == "__main__":
    unittest.main()
