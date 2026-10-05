from __future__ import annotations

import asyncio
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

from pinit.api.services import location_photos as lp
from pinit.config import secrets


def _photos(n):
    return [{"name": f"places/p/photos/{i}"} for i in range(n)]


def _row(**overrides):
    row = {
        "location_id": 7,
        "google_place_id": "gp-7",
        "image_stored": False,
        "image_unavailable": False,
        "extra_photos_stored": 0,
        "photos": _photos(5),
    }
    row.update(overrides)
    return row


def _supabase(row):
    supabase = MagicMock()
    supabase.get_location.return_value = row
    return supabase


class _Enqueue:
    def __init__(self):
        self.jobs = []

    async def __call__(self, name, handler):
        self.jobs.append((name, handler))


def _download_ok(name):
    return lambda: (f"bytes:{name}".encode(), "image/jpeg")


class RowHelpersTest(unittest.TestCase):
    def test_photo_names_reads_list_or_json_and_skips_blanks(self):
        self.assertEqual(lp.photo_names({"photos": _photos(2)}), ["places/p/photos/0", "places/p/photos/1"])
        self.assertEqual(lp.photo_names({"photos": '[{"name": "a"}, {"name": " "}]'}), ["a"])
        self.assertEqual(lp.photo_names({"photos": None}), [])

    def test_stored_count_is_primary_plus_extras(self):
        self.assertEqual(lp.stored_count({"image_stored": False, "extra_photos_stored": 4}), 0)
        self.assertEqual(lp.stored_count({"image_stored": True, "extra_photos_stored": None}), 1)
        self.assertEqual(lp.stored_count({"image_stored": True, "extra_photos_stored": 3}), 4)


class EnsureLocationPhotosTest(unittest.TestCase):
    def setUp(self):
        redis = patch.object(lp, "_redis", return_value=None)
        redis.start()
        self.addCleanup(redis.stop)
        lp._local_locks.clear()

    def test_new_place_stores_primary_and_two_extras_contiguously(self):
        supabase = _supabase(_row())
        with patch.object(lp, "_from_name", side_effect=_download_ok):
            stored = asyncio.run(lp.ensure_location_photos(7, 3, supabase=supabase))

        self.assertEqual(stored, 3)
        indexes = [c.args[3] for c in supabase.upload_location_photo.call_args_list]
        self.assertEqual(indexes, [None, 1, 2])
        supabase.mark_location_image_uploaded.assert_called_once()
        self.assertEqual(supabase.mark_location_image_uploaded.call_args.args[2], "places/p/photos/0")
        supabase.mark_location_extra_photos_stored.assert_called_once_with(7, 2)

    def test_tops_up_extras_without_touching_the_primary(self):
        supabase = _supabase(_row(image_stored=True, extra_photos_stored=1))
        with patch.object(lp, "_from_name", side_effect=_download_ok) as from_name:
            stored = asyncio.run(lp.ensure_location_photos(7, 4, supabase=supabase))

        self.assertEqual(stored, 4)
        self.assertCountEqual([c.args[0] for c in from_name.call_args_list], ["places/p/photos/2", "places/p/photos/3"])
        indexes = [c.args[3] for c in supabase.upload_location_photo.call_args_list]
        self.assertEqual(indexes, [2, 3])
        supabase.mark_location_image_uploaded.assert_not_called()
        supabase.mark_location_extra_photos_stored.assert_called_once_with(7, 3)

    def test_a_failed_download_stops_so_stored_photos_stay_contiguous(self):
        supabase = _supabase(_row())

        def from_name(name):
            if name.endswith("/1"):
                return lambda: None
            return _download_ok(name)

        with patch.object(lp, "_from_name", side_effect=from_name):
            stored = asyncio.run(lp.ensure_location_photos(7, 3, supabase=supabase))

        self.assertEqual(stored, 1)
        self.assertEqual(supabase.upload_location_photo.call_count, 1)
        supabase.mark_location_extra_photos_stored.assert_not_called()

    def test_already_satisfied_downloads_nothing(self):
        supabase = _supabase(_row(image_stored=True, extra_photos_stored=2))
        with patch.object(lp, "_from_name") as from_name:
            stored = asyncio.run(lp.ensure_location_photos(7, 3, supabase=supabase))
        self.assertEqual(stored, 3)
        from_name.assert_not_called()

    def test_google_with_no_photos_marks_unavailable_without_a_details_call(self):
        supabase = _supabase(_row(photos=None))
        with patch.object(lp, "_refresh_details") as refresh:
            stored = asyncio.run(lp.ensure_location_photos(7, 3, supabase=supabase, photos=[]))
        self.assertEqual(stored, 0)
        refresh.assert_not_called()
        supabase.mark_location_image_unavailable.assert_called_once_with(7)

    def test_row_without_metadata_refreshes_details_once(self):
        supabase = _supabase(_row(photos=None))
        with patch.object(lp, "_refresh_details", return_value={"photos": _photos(2)}) as refresh, \
                patch.object(lp, "_from_name", side_effect=_download_ok):
            stored = asyncio.run(lp.ensure_location_photos(7, 3, supabase=supabase))
        refresh.assert_called_once()
        self.assertEqual(stored, 2)

    def test_tap_links_are_downloaded_instead_of_new_google_requests(self):
        supabase = _supabase(_row(image_stored=True))
        pairs = [("places/p/photos/1", "https://lh3/a"), ("places/p/photos/2", "https://lh3/b")]
        with patch.object(lp, "_fetch_uri", return_value=(b"x", "image/jpeg")) as fetch_uri, \
                patch.object(lp, "_from_name") as from_name:
            stored = asyncio.run(lp.ensure_location_photos(7, 3, supabase=supabase, photo_uris=pairs))
        self.assertEqual(stored, 3)
        # Downloads run in parallel; uploads keep display order.
        self.assertCountEqual([c.args[0] for c in fetch_uri.call_args_list], ["https://lh3/a", "https://lh3/b"])
        self.assertEqual([c.args[3] for c in supabase.upload_location_photo.call_args_list], [1, 2])
        from_name.assert_not_called()

    def test_concurrent_call_for_the_same_place_is_skipped(self):
        supabase = _supabase(_row())
        release = lp._acquire(7)
        try:
            stored = asyncio.run(lp.ensure_location_photos(7, 3, supabase=supabase))
        finally:
            release()
        self.assertEqual(stored, -1)
        supabase.get_location.assert_not_called()

    def test_redis_lock_is_shared_across_workers(self):
        client = MagicMock()
        client.set.return_value = False
        with patch.object(lp, "_redis", return_value=client):
            self.assertIsNone(lp._acquire(7))
        client.set.assert_called_once()
        self.assertEqual(client.set.call_args.kwargs, {"nx": True, "ex": lp.LOCK_TTL_SECONDS})


class ScheduleMissingPhotosTest(unittest.TestCase):
    def test_queues_only_places_without_photos_capped_per_request(self):
        rows = [{"location_id": 1, "image_stored": True}, {"location_id": 2, "image_unavailable": True}]
        rows += [{"location_id": 100 + i, "image_stored": False} for i in range(15)]
        enqueue = _Enqueue()
        queued = asyncio.run(lp.schedule_missing_photos(rows, enqueue))
        self.assertEqual(queued, lp.MAX_BACKGROUND_PER_REQUEST)
        names = [name for name, _ in enqueue.jobs]
        self.assertEqual(names[0], "location:100:photos")
        self.assertNotIn("location:1:photos", names)
        self.assertNotIn("location:2:photos", names)


class GalleryForTapTest(unittest.TestCase):
    def setUp(self):
        patched = patch.multiple(
            secrets,
            PHOTO_CDN_BASE_URL="https://img.example.com",
            SUPABASE_URL="https://proj.supabase.co",
        )
        patched.start()
        self.addCleanup(patched.stop)
        r2 = patch.object(lp.r2_photos, "is_configured", return_value=True)
        r2.start()
        self.addCleanup(r2.stop)

    def test_stored_cdn_urls_then_google_links_and_queues_the_copy(self):
        supabase = _supabase(_row(image_stored=True, photos=_photos(4)))
        enqueue = _Enqueue()
        with patch.object(lp, "google_photo_uri", side_effect=lambda name: f"https://lh3/{name[-1]}"):
            urls = asyncio.run(lp.gallery_for_tap(7, 10, supabase=supabase, enqueue=enqueue))

        self.assertEqual(urls, [
            "https://img.example.com/l/7/0_hero.webp",
            "https://lh3/1",
            "https://lh3/2",
            "https://lh3/3",
        ])
        self.assertEqual([name for name, _ in enqueue.jobs], ["location:7:photos"])

    def test_a_failed_link_truncates_so_order_matches_what_gets_stored(self):
        supabase = _supabase(_row(image_stored=True, photos=_photos(4)))
        enqueue = _Enqueue()

        def uri(name):
            return None if name.endswith("/2") else f"https://lh3/{name[-1]}"

        with patch.object(lp, "google_photo_uri", side_effect=uri):
            urls = asyncio.run(lp.gallery_for_tap(7, 10, supabase=supabase, enqueue=enqueue))
        self.assertEqual(urls, ["https://img.example.com/l/7/0_hero.webp", "https://lh3/1"])

    def test_fully_stored_gallery_makes_no_google_calls(self):
        supabase = _supabase(_row(image_stored=True, extra_photos_stored=2, photos=_photos(3)))
        enqueue = _Enqueue()
        with patch.object(lp, "google_photo_uri") as google:
            urls = asyncio.run(lp.gallery_for_tap(7, 10, supabase=supabase, enqueue=enqueue))
        self.assertEqual(len(urls), 3)
        google.assert_not_called()
        self.assertEqual(enqueue.jobs, [])

    def test_no_metadata_returns_quickly_and_prefetches_in_background(self):
        supabase = _supabase(_row(photos=None))
        enqueue = _Enqueue()
        with patch.object(lp, "google_photo_uri") as google:
            urls = asyncio.run(lp.gallery_for_tap(7, 10, supabase=supabase, enqueue=enqueue))
        self.assertEqual(urls, [])
        google.assert_not_called()
        self.assertEqual(len(enqueue.jobs), 1)

    def test_unavailable_and_missing_places(self):
        enqueue = _Enqueue()
        self.assertEqual(
            asyncio.run(lp.gallery_for_tap(7, 10, supabase=_supabase(_row(image_unavailable=True)), enqueue=enqueue)),
            [],
        )
        self.assertIsNone(asyncio.run(lp.gallery_for_tap(7, 10, supabase=_supabase(None), enqueue=enqueue)))

    def test_without_r2_stored_photos_use_supabase_storage(self):
        supabase = _supabase(_row(image_stored=True, extra_photos_stored=1, photos=_photos(2)))
        with patch.object(lp.r2_photos, "is_configured", return_value=False):
            urls = asyncio.run(lp.gallery_for_tap(7, 10, supabase=supabase, enqueue=_Enqueue()))
        self.assertEqual(urls, [
            "https://proj.supabase.co/storage/v1/object/public/location_photos/7.jpg",
            "https://proj.supabase.co/storage/v1/object/public/location_photos/7_1.jpg",
        ])


if __name__ == "__main__":
    unittest.main()
