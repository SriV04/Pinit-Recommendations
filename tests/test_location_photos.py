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


class _Dispatch:
    """Records the ``photos`` tasks handed to the worker."""

    def __init__(self):
        self.payloads = []

    async def __call__(self, payload):
        self.payloads.append(payload)

    @property
    def ids(self):
        return [p.location_id for p in self.payloads]


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
        with patch.object(lp, "_refresh_photo_names") as refresh:
            stored = asyncio.run(lp.ensure_location_photos(7, 3, supabase=supabase, photos=[]))
        self.assertEqual(stored, 0)
        refresh.assert_not_called()
        supabase.mark_location_image_unavailable.assert_called_once_with(7)

    def test_row_without_metadata_refreshes_details_once(self):
        supabase = _supabase(_row(photos=None))
        with patch.object(lp, "_refresh_photo_names", return_value={"photos": _photos(2)}) as refresh, \
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
        dispatch = _Dispatch()
        queued = asyncio.run(lp.schedule_missing_photos(rows, dispatch))
        self.assertEqual(queued, lp.MAX_BACKGROUND_PER_REQUEST)
        self.assertEqual(dispatch.ids[0], 100)
        self.assertNotIn(1, dispatch.ids)
        self.assertNotIn(2, dispatch.ids)
        self.assertTrue(all(p.task_type == "photos" for p in dispatch.payloads))
        self.assertTrue(all(p.want == lp.LIST_PHOTOS == 1 for p in dispatch.payloads))


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
        dispatch = _Dispatch()
        with patch.object(lp, "google_photo_uri", side_effect=lambda name: f"https://lh3/{name[-1]}"):
            urls = asyncio.run(lp.gallery_for_tap(7, 10, supabase=supabase, dispatch=dispatch))

        self.assertEqual(urls, [
            "https://img.example.com/l/7/0_hero.webp",
            "https://lh3/1",
            "https://lh3/2",
            "https://lh3/3",
        ])
        self.assertEqual(dispatch.ids, [7])
        job = dispatch.payloads[0]
        self.assertEqual(job.want, 4)
        self.assertEqual(
            [tuple(p) for p in job.photo_uris],
            [(f"places/p/photos/{i}", f"https://lh3/{i}") for i in (1, 2, 3)],
        )

    def test_a_failed_link_truncates_so_order_matches_what_gets_stored(self):
        supabase = _supabase(_row(image_stored=True, photos=_photos(4)))
        dispatch = _Dispatch()

        def uri(name):
            return None if name.endswith("/2") else f"https://lh3/{name[-1]}"

        with patch.object(lp, "google_photo_uri", side_effect=uri):
            urls = asyncio.run(lp.gallery_for_tap(7, 10, supabase=supabase, dispatch=dispatch))
        self.assertEqual(urls, ["https://img.example.com/l/7/0_hero.webp", "https://lh3/1"])

    def test_fully_stored_gallery_makes_no_google_calls(self):
        supabase = _supabase(_row(image_stored=True, extra_photos_stored=2, photos=_photos(3)))
        dispatch = _Dispatch()
        with patch.object(lp, "google_photo_uri") as google:
            urls = asyncio.run(lp.gallery_for_tap(7, 10, supabase=supabase, dispatch=dispatch))
        self.assertEqual(len(urls), 3)
        google.assert_not_called()
        self.assertEqual(dispatch.payloads, [])

    def test_no_metadata_fetches_details_inline_and_returns_photos(self):
        supabase = _supabase(_row(photos=None))
        dispatch = _Dispatch()
        with patch.object(lp, "_refresh_photo_names", return_value={"photos": _photos(2)}) as details, \
                patch.object(lp, "google_photo_uri", side_effect=lambda name: f"https://lh3/{name[-1]}"):
            urls = asyncio.run(lp.gallery_for_tap(7, 10, supabase=supabase, dispatch=dispatch))
        details.assert_called_once()
        self.assertEqual(urls, ["https://lh3/0", "https://lh3/1"])
        self.assertEqual(dispatch.ids, [7])
        self.assertEqual(dispatch.payloads[0].want, 2)

    def test_no_metadata_and_details_fail_queues_the_full_step(self):
        supabase = _supabase(_row(photos=None))
        dispatch = _Dispatch()
        with patch.object(lp, "_refresh_photo_names", return_value=None), \
                patch.object(lp, "google_photo_uri") as google:
            urls = asyncio.run(lp.gallery_for_tap(7, 10, supabase=supabase, dispatch=dispatch))
        self.assertEqual(urls, [])
        google.assert_not_called()
        self.assertEqual(dispatch.ids, [7])
        self.assertEqual(dispatch.payloads[0].photo_uris, [])

    def test_details_with_no_photos_marks_unavailable(self):
        supabase = _supabase(_row(photos=None))
        with patch.object(lp, "_refresh_photo_names", return_value={"photos": []}):
            urls = asyncio.run(lp.gallery_for_tap(7, 10, supabase=supabase, dispatch=_Dispatch()))
        self.assertEqual(urls, [])
        supabase.mark_location_image_unavailable.assert_called_once_with(7)

    def test_unavailable_and_missing_places(self):
        dispatch = _Dispatch()
        self.assertEqual(
            asyncio.run(lp.gallery_for_tap(7, 10, supabase=_supabase(_row(image_unavailable=True)), dispatch=dispatch)),
            [],
        )
        self.assertIsNone(asyncio.run(lp.gallery_for_tap(7, 10, supabase=_supabase(None), dispatch=dispatch)))

    def test_without_r2_stored_photos_use_supabase_storage(self):
        supabase = _supabase(_row(image_stored=True, extra_photos_stored=1, photos=_photos(2)))
        with patch.object(lp.r2_photos, "is_configured", return_value=False):
            urls = asyncio.run(lp.gallery_for_tap(7, 10, supabase=supabase, dispatch=_Dispatch()))
        self.assertEqual(urls, [
            "https://proj.supabase.co/storage/v1/object/public/location_photos/7.jpg",
            "https://proj.supabase.co/storage/v1/object/public/location_photos/7_1.jpg",
        ])


class EnsurePrimaryPhotosTest(unittest.TestCase):
    def setUp(self):
        patched = patch.multiple(secrets, PHOTO_CDN_BASE_URL="https://img.example.com")
        patched.start()
        self.addCleanup(patched.stop)
        r2 = patch.object(lp.r2_photos, "is_configured", return_value=True)
        r2.start()
        self.addCleanup(r2.stop)

    def test_stored_link_or_queue_per_place(self):
        supabase = MagicMock()
        supabase.get_locations_by_ids.return_value = [
            _row(location_id=1, image_stored=True),
            _row(location_id=2),
            _row(location_id=3, photos=None),
            _row(location_id=4, image_unavailable=True),
        ]
        dispatch = _Dispatch()
        with patch.object(lp, "google_photo_uri", return_value="https://lh3/x") as google:
            urls = asyncio.run(lp.ensure_primary_photos([1, 2, 3, 4, 2], supabase=supabase, dispatch=dispatch))

        self.assertEqual(urls, {1: "https://img.example.com/l/1/0_hero.webp", 2: "https://lh3/x"})
        google.assert_called_once_with("places/p/photos/0")
        supabase.get_locations_by_ids.assert_called_once_with([1, 2, 3, 4])
        self.assertEqual(sorted(dispatch.ids), [2, 3])
        by_id = {p.location_id: p for p in dispatch.payloads}
        self.assertEqual([tuple(p) for p in by_id[2].photo_uris], [("places/p/photos/0", "https://lh3/x")])
        self.assertEqual(by_id[3].photo_uris, [])
        self.assertEqual({p.want for p in dispatch.payloads}, {lp.LIST_PHOTOS})

    def test_a_failed_link_still_queues_storage(self):
        supabase = MagicMock()
        supabase.get_locations_by_ids.return_value = [_row(location_id=2)]
        dispatch = _Dispatch()
        with patch.object(lp, "google_photo_uri", return_value=None):
            urls = asyncio.run(lp.ensure_primary_photos([2], supabase=supabase, dispatch=dispatch))
        self.assertEqual(urls, {})
        self.assertEqual(dispatch.ids, [2])

    def test_caps_ids_per_call(self):
        supabase = MagicMock()
        supabase.get_locations_by_ids.return_value = []
        asyncio.run(lp.ensure_primary_photos(list(range(100)), supabase=supabase, dispatch=_Dispatch()))
        self.assertEqual(len(supabase.get_locations_by_ids.call_args.args[0]), lp.MAX_ENSURE_IDS)


class EnsureWithLinksTest(unittest.TestCase):
    def test_links_matched_by_name_and_expired_link_falls_back_to_name(self):
        supabase = _supabase(_row(image_stored=True, photos=_photos(4)))
        fetched = []

        def fetch_uri(uri):
            fetched.append(uri)
            return None if uri.endswith("/2") else (b"x", "image/jpeg")

        def by_name(name):
            def fetch():
                fetched.append(name)
                return (b"y", "image/jpeg")
            return fetch

        pairs = [("places/p/photos/2", "https://lh3/2"), ("places/p/photos/1", "https://lh3/1")]
        with patch.object(lp, "_acquire", return_value=lambda: None), \
                patch.object(lp, "_fetch_uri", side_effect=fetch_uri), \
                patch.object(lp, "_from_name", side_effect=by_name):
            stored = asyncio.run(lp.ensure_location_photos(7, 3, supabase=supabase, photo_uris=pairs))

        self.assertEqual(stored, 3)
        self.assertEqual(sorted(fetched), ["https://lh3/1", "https://lh3/2", "places/p/photos/2"])
        indices = [c.args[3] for c in supabase.upload_location_photo.call_args_list]
        self.assertEqual(indices, [1, 2])

    def test_links_without_names_are_stored_without_a_details_call(self):
        supabase = _supabase(_row(photos=None))
        with patch.object(lp, "_acquire", return_value=lambda: None), \
                patch.object(lp, "_refresh_photo_names") as details, \
                patch.object(lp, "_fetch_uri", return_value=(b"x", "image/jpeg")):
            stored = asyncio.run(
                lp.ensure_location_photos(7, 1, supabase=supabase, photo_uris=[("places/p/photos/0", "https://lh3/0")])
            )
        self.assertEqual(stored, 1)
        details.assert_not_called()


def _fresh(n):
    return [{"name": f"places/p/photos/fresh{i}"} for i in range(n)]


class StalePhotoNamesTest(unittest.TestCase):
    def test_google_400_invalid_argument_means_stale(self):
        response = MagicMock(status_code=400, text='{"error": {"status": "INVALID_ARGUMENT"}}')
        with patch.object(lp.requests, "get", return_value=response):
            with self.assertRaises(lp.StalePhotoName):
                lp.google_photo_uri("places/p/photos/0")

    def test_expired_names_refresh_details_once_and_store_fresh_ones(self):
        supabase = _supabase(_row(photos=_photos(3)))

        def by_name(name):
            def fetch():
                if "fresh" not in name:
                    raise lp.StalePhotoName(name)
                return (name.encode(), "image/jpeg")
            return fetch

        with patch.object(lp, "_acquire", return_value=lambda: None), \
                patch.object(lp, "_from_name", side_effect=by_name), \
                patch.object(lp, "_refresh_photo_names", return_value={"photos": _fresh(3)}) as details:
            stored = asyncio.run(lp.ensure_location_photos(7, 3, supabase=supabase))

        self.assertEqual(stored, 3)
        details.assert_called_once()
        uploaded = [c.args[1] for c in supabase.upload_location_photo.call_args_list]
        self.assertEqual(uploaded, [f"places/p/photos/fresh{i}".encode() for i in range(3)])
        self.assertEqual(supabase.mark_location_image_uploaded.call_args.args[2], "places/p/photos/fresh0")

    def test_still_stale_after_refresh_gives_up_without_looping(self):
        supabase = _supabase(_row(photos=_photos(3)))

        def stale(name):
            def fetch():
                raise lp.StalePhotoName(name)
            return fetch

        with patch.object(lp, "_acquire", return_value=lambda: None), \
                patch.object(lp, "_from_name", side_effect=stale), \
                patch.object(lp, "_refresh_photo_names", return_value={"photos": _fresh(3)}) as details:
            stored = asyncio.run(lp.ensure_location_photos(7, 3, supabase=supabase))
        self.assertEqual(stored, 0)
        details.assert_called_once()
        supabase.upload_location_photo.assert_not_called()

    def test_refresh_with_no_photos_marks_unavailable(self):
        supabase = _supabase(_row(photos=_photos(2)))

        def stale(name):
            def fetch():
                raise lp.StalePhotoName(name)
            return fetch

        with patch.object(lp, "_acquire", return_value=lambda: None), \
                patch.object(lp, "_from_name", side_effect=stale), \
                patch.object(lp, "_refresh_photo_names", return_value={"photos": []}):
            self.assertEqual(asyncio.run(lp.ensure_location_photos(7, 3, supabase=supabase)), 0)
        supabase.mark_location_image_unavailable.assert_called_once_with(7)

    def test_tap_with_expired_names_refreshes_inline(self):
        supabase = _supabase(_row(photos=_photos(2)))
        dispatch = _Dispatch()

        def uri(name):
            if "fresh" not in name:
                raise lp.StalePhotoName(name)
            return f"https://lh3/{name[-1]}"

        with patch.object(lp, "google_photo_uri", side_effect=uri), \
                patch.object(lp, "_refresh_photo_names", return_value={"photos": _fresh(2)}) as details:
            urls = asyncio.run(lp.gallery_for_tap(7, 10, supabase=supabase, dispatch=dispatch))
        details.assert_called_once()
        self.assertEqual(urls, ["https://lh3/0", "https://lh3/1"])
        self.assertEqual([p[0] for p in dispatch.payloads[0].photo_uris],
                         ["places/p/photos/fresh0", "places/p/photos/fresh1"])

    def test_list_ensure_with_expired_name_queues_the_step(self):
        supabase = MagicMock()
        supabase.get_locations_by_ids.return_value = [_row(location_id=2)]
        dispatch = _Dispatch()
        with patch.object(lp, "google_photo_uri", side_effect=lp.StalePhotoName("x")):
            urls = asyncio.run(lp.ensure_primary_photos([2], supabase=supabase, dispatch=dispatch))
        self.assertEqual(urls, {})
        self.assertEqual(dispatch.ids, [2])
        self.assertEqual(dispatch.payloads[0].photo_uris, [])


class RefreshPhotoNamesTest(unittest.TestCase):
    def test_requests_only_the_free_id_and_photos_fields_and_saves_them(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"id": "gp-7", "photos": _fresh(2)}
        supabase = MagicMock()
        with patch.object(lp.requests, "get", return_value=response) as get:
            result = lp._refresh_photo_names(7, _row(), supabase)

        self.assertEqual(result, {"photos": _fresh(2)})
        self.assertEqual(get.call_args.kwargs["headers"]["X-Goog-FieldMask"], "id,photos")
        self.assertTrue(get.call_args.args[0].endswith("/places/gp-7"))
        supabase.update_location.assert_called_once_with(7, photos=_fresh(2))

    def test_no_place_id_or_failure_returns_none(self):
        self.assertIsNone(lp._refresh_photo_names(7, _row(google_place_id=None), MagicMock()))
        with patch.object(lp.requests, "get", side_effect=RuntimeError("down")):
            self.assertIsNone(lp._refresh_photo_names(7, _row(), MagicMock()))


if __name__ == "__main__":
    unittest.main()
