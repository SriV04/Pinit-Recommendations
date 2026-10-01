from __future__ import annotations

import io
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

from PIL import Image

from pinit.config import secrets
from pinit.integrations import r2_photos
from pinit.integrations.supabase import SupabaseService


def _image_bytes(size=(2000, 1000), mode="RGB", fmt="JPEG", color=(200, 80, 60), **save_kwargs) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format=fmt, **save_kwargs)
    return buffer.getvalue()


def _decode(webp: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(webp))
    image.load()
    return image


R2_SETTINGS = {
    "R2_ACCOUNT_ID": "acct",
    "R2_ACCESS_KEY_ID": "key",
    "R2_SECRET_ACCESS_KEY": "secret",
    "R2_BUCKET_NAME": "pinit-photos",
}


def _r2_configured():
    return patch.multiple(secrets, **R2_SETTINGS)


def _r2_unconfigured():
    return patch.multiple(secrets, **{name: "" for name in R2_SETTINGS})


class BuildVariantsTests(unittest.TestCase):
    def test_large_image_gets_each_target_width_and_keeps_aspect(self):
        variants = r2_photos.build_variants(_image_bytes(size=(2000, 1000)))

        self.assertEqual(set(variants), {"thumb", "card", "hero"})
        self.assertEqual(_decode(variants["thumb"]).size, (320, 160))
        self.assertEqual(_decode(variants["card"]).size, (720, 360))
        self.assertEqual(_decode(variants["hero"]).size, (1440, 720))
        for body in variants.values():
            self.assertEqual(_decode(body).format, "WEBP")

    def test_never_upscales_small_sources(self):
        variants = r2_photos.build_variants(_image_bytes(size=(500, 400)))

        self.assertEqual(_decode(variants["thumb"]).size, (320, 256))
        self.assertEqual(_decode(variants["card"]).size, (500, 400))
        self.assertEqual(_decode(variants["hero"]).size, (500, 400))

    def test_transparent_png_is_flattened_onto_cream(self):
        source = _image_bytes(size=(400, 400), mode="RGBA", fmt="PNG", color=(0, 0, 0, 0))
        rendered = _decode(r2_photos.build_variants(source)["card"]).convert("RGB")

        r, g, b = rendered.getpixel((200, 200))
        # Cream backdrop (251, 246, 243); WebP is lossy so allow a small delta.
        self.assertLess(abs(r - 251) + abs(g - 246) + abs(b - 243), 12)

    def test_exif_orientation_is_applied(self):
        image = Image.new("RGB", (800, 400), (10, 120, 200))
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90 CW when displayed
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", exif=exif)

        card = _decode(r2_photos.build_variants(buffer.getvalue())["card"])

        self.assertEqual(card.size, (400, 800))

    def test_undecodable_bytes_raise_value_error(self):
        with self.assertRaises(ValueError):
            r2_photos.build_variants(b"not an image")

    def test_variants_are_much_smaller_than_the_original(self):
        original = _image_bytes(size=(1600, 1600), quality=95)
        variants = r2_photos.build_variants(original)

        self.assertLess(len(variants["thumb"]), len(original) / 5)


class KeyTests(unittest.TestCase):
    def test_variant_key_matches_client_contract(self):
        self.assertEqual(r2_photos.variant_key(42, 0, "thumb"), "l/42/0_thumb.webp")
        self.assertEqual(r2_photos.variant_key(42, 3, "hero"), "l/42/3_hero.webp")

    def test_unknown_size_is_rejected(self):
        with self.assertRaises(ValueError):
            r2_photos.variant_key(42, 0, "huge")

    def test_original_key_follows_content_type(self):
        self.assertEqual(r2_photos.original_key(7, 0, "image/png"), "l/7/0_orig.png")
        self.assertEqual(r2_photos.original_key(7, 2, "image/jpeg; charset=x"), "l/7/2_orig.jpg")
        self.assertEqual(r2_photos.original_key(7, 0, None), "l/7/0_orig.jpg")
        self.assertEqual(r2_photos.original_key(7, 0, "image/gif"), "l/7/0_orig.jpg")


class ConfigTests(unittest.TestCase):
    def test_is_configured_requires_every_setting(self):
        with _r2_configured():
            self.assertTrue(r2_photos.is_configured())
        with patch.multiple(secrets, **{**R2_SETTINGS, "R2_BUCKET_NAME": ""}):
            self.assertFalse(r2_photos.is_configured())
        with _r2_unconfigured():
            self.assertFalse(r2_photos.is_configured())

    def test_ingest_size_only_grows_when_r2_is_on(self):
        with _r2_configured():
            self.assertEqual(r2_photos.ingest_max_px(), 1600)
        with _r2_unconfigured():
            self.assertEqual(r2_photos.ingest_max_px(), 800)

    def test_dual_write_defaults_on_and_can_be_disabled(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("PHOTO_DUAL_WRITE_SUPABASE", None)
            self.assertTrue(r2_photos.dual_write_supabase())
        with patch.dict("os.environ", {"PHOTO_DUAL_WRITE_SUPABASE": "false"}):
            self.assertFalse(r2_photos.dual_write_supabase())


class UploadTests(unittest.TestCase):
    def _upload(self, **kwargs):
        client = MagicMock()
        with _r2_configured(), patch.object(r2_photos, "_client", return_value=client):
            r2_photos.upload_location_photo(**kwargs)
        return client

    def test_primary_upload_writes_three_variants_and_the_original(self):
        source = _image_bytes()
        client = self._upload(
            location_id=42, image_bytes=source, content_type="image/jpeg", index=None
        )

        calls = {c.kwargs["Key"]: c.kwargs for c in client.put_object.call_args_list}
        self.assertEqual(
            set(calls),
            {"l/42/0_thumb.webp", "l/42/0_card.webp", "l/42/0_hero.webp", "l/42/0_orig.jpg"},
        )
        for key, kwargs in calls.items():
            self.assertEqual(kwargs["Bucket"], "pinit-photos")
            self.assertEqual(kwargs["CacheControl"], r2_photos.IMMUTABLE_CACHE_CONTROL)
            if key.endswith(".webp"):
                self.assertEqual(kwargs["ContentType"], "image/webp")
        self.assertEqual(calls["l/42/0_orig.jpg"]["Body"], source)
        self.assertEqual(calls["l/42/0_orig.jpg"]["ContentType"], "image/jpeg")

    def test_extra_photo_uses_its_index(self):
        client = self._upload(
            location_id=42, image_bytes=_image_bytes(), content_type="image/jpeg", index=3
        )

        keys = {c.kwargs["Key"] for c in client.put_object.call_args_list}
        self.assertIn("l/42/3_card.webp", keys)
        self.assertNotIn("l/42/0_card.webp", keys)

    def test_failed_put_propagates(self):
        client = MagicMock()
        client.put_object.side_effect = RuntimeError("r2 down")
        with _r2_configured(), patch.object(r2_photos, "_client", return_value=client):
            with self.assertRaises(RuntimeError):
                r2_photos.upload_location_photo(42, _image_bytes(), "image/jpeg")

    def test_corrupt_image_uploads_nothing(self):
        client = MagicMock()
        with _r2_configured(), patch.object(r2_photos, "_client", return_value=client):
            with self.assertRaises(ValueError):
                r2_photos.upload_location_photo(42, b"garbage", "image/jpeg")
        client.put_object.assert_not_called()


class SupabaseServiceRoutingTests(unittest.TestCase):
    def _service(self):
        service = SupabaseService.__new__(SupabaseService)
        service.client = MagicMock()
        return service

    def test_without_r2_config_only_supabase_storage_is_written(self):
        service = self._service()
        with _r2_unconfigured(), patch.object(r2_photos, "upload_location_photo") as r2_upload:
            service.upload_location_photo(5, b"bytes", "image/jpeg")

        r2_upload.assert_not_called()
        service.client.storage.from_.assert_called_with("location_photos")

    def test_dual_write_writes_r2_first_then_supabase(self):
        service = self._service()
        order = []
        with (
            _r2_configured(),
            patch.dict("os.environ", {"PHOTO_DUAL_WRITE_SUPABASE": "true"}),
            patch.object(
                r2_photos, "upload_location_photo", side_effect=lambda *a, **k: order.append("r2")
            ),
        ):
            service.client.storage.from_.return_value.upload.side_effect = (
                lambda *a, **k: order.append("supabase")
            )
            service.upload_location_photo(5, b"bytes", "image/jpeg", 2)

        self.assertEqual(order, ["r2", "supabase"])

    def test_r2_only_mode_skips_supabase(self):
        service = self._service()
        with (
            _r2_configured(),
            patch.dict("os.environ", {"PHOTO_DUAL_WRITE_SUPABASE": "false"}),
            patch.object(r2_photos, "upload_location_photo") as r2_upload,
        ):
            service.upload_location_photo(5, b"bytes", "image/png")

        r2_upload.assert_called_once_with(5, b"bytes", "image/png", None)
        service.client.storage.from_.assert_not_called()

    def test_r2_failure_raises_and_leaves_supabase_untouched(self):
        service = self._service()
        with (
            _r2_configured(),
            patch.object(r2_photos, "upload_location_photo", side_effect=RuntimeError("boom")),
        ):
            with self.assertRaises(RuntimeError):
                service.upload_location_photo(5, b"bytes", "image/jpeg")

        service.client.storage.from_.assert_not_called()


if __name__ == "__main__":
    unittest.main()
