"""Cloudflare R2 storage for location photos.

Key layout (shared contract with the Flutter client's ``PhotoUrls``):

    l/{location_id}/{index}_{thumb|card|hero}.webp   immutable WebP variants
    l/{location_id}/{index}_orig.{ext}               untouched source bytes

``index`` 0 is the primary photo, 1..9 are extras. Objects are immutable and
served through the Cloudflare CDN (``PHOTO_CDN_BASE_URL``).

Nothing here touches ``image_stored`` / ``photos`` / ``extra_photos_stored``;
callers keep using the ``mark_location_*`` RPCs after a successful upload.
"""
from __future__ import annotations

import io
import logging
import os
from functools import lru_cache
from typing import Any, Dict, Optional

from PIL import Image, ImageOps

from pinit.config import secrets

logger = logging.getLogger(__name__)

# Size role -> max width in px. Mirrors PhotoSize in the Flutter client.
PHOTO_VARIANTS: Dict[str, int] = {"thumb": 320, "card": 720, "hero": 1440}

# Download size from Google when R2 is on, so the hero variant is not an upscale.
_R2_INGEST_MAX_PX = 1600
_LEGACY_INGEST_MAX_PX = 800

WEBP_QUALITY = 80
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"

# Pinit cream (never pure white), used behind transparent images.
_BACKDROP_RGB = (251, 246, 243)

_ORIGINAL_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def is_configured() -> bool:
    """True when every R2 setting is present."""
    return all(
        (
            secrets.R2_ACCOUNT_ID,
            secrets.R2_ACCESS_KEY_ID,
            secrets.R2_SECRET_ACCESS_KEY,
            secrets.R2_BUCKET_NAME,
        )
    )


def dual_write_supabase() -> bool:
    """Keep writing to Supabase Storage until cutover (old app builds read it)."""
    return os.getenv("PHOTO_DUAL_WRITE_SUPABASE", "true").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def ingest_max_px() -> int:
    """Pixel cap to request from Google Places for new photos."""
    return _R2_INGEST_MAX_PX if is_configured() else _LEGACY_INGEST_MAX_PX


def variant_key(location_id: int, index: int, size: str) -> str:
    if size not in PHOTO_VARIANTS:
        raise ValueError(f"unknown photo size {size!r}")
    return f"l/{int(location_id)}/{int(index)}_{size}.webp"


def original_key(location_id: int, index: int, content_type: Optional[str]) -> str:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    extension = _ORIGINAL_EXTENSIONS.get(normalized, ".jpg")
    return f"l/{int(location_id)}/{int(index)}_orig{extension}"


def build_variants(image_bytes: bytes) -> Dict[str, bytes]:
    """Encode the thumb/card/hero WebP variants. Never upscales.

    Raises ``ValueError`` if the bytes are not a decodable image.
    """
    try:
        source = Image.open(io.BytesIO(image_bytes))
        source.load()
    except Exception as exc:
        raise ValueError(f"cannot decode image: {exc}") from exc

    with source:
        image = ImageOps.exif_transpose(source)
        has_alpha = image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        )
        if has_alpha:
            rgba = image.convert("RGBA")
            flattened = Image.new("RGB", rgba.size, _BACKDROP_RGB)
            flattened.paste(rgba, mask=rgba.getchannel("A"))
            image = flattened
        else:
            image = image.convert("RGB")

        variants: Dict[str, bytes] = {}
        for size, max_width in PHOTO_VARIANTS.items():
            width = min(max_width, image.width)
            if width == image.width:
                resized = image
            else:
                height = max(1, round(image.height * width / image.width))
                resized = image.resize((width, height), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            resized.save(buffer, format="WEBP", quality=WEBP_QUALITY, method=4)
            variants[size] = buffer.getvalue()
        return variants


@lru_cache(maxsize=1)
def _client() -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{secrets.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=secrets.R2_ACCESS_KEY_ID,
        aws_secret_access_key=secrets.R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )


def upload_location_photo(
    location_id: int,
    image_bytes: bytes,
    content_type: Optional[str] = None,
    index: Optional[int] = None,
) -> None:
    """Upload the original plus all WebP variants for one location photo.

    ``index`` ``None`` or ``0`` is the primary photo. Raises on any failure so
    callers do not mark the photo as stored.
    """
    photo_index = 0 if index is None else int(index)
    variants = build_variants(image_bytes)
    client = _client()
    bucket = secrets.R2_BUCKET_NAME

    for size, body in variants.items():
        client.put_object(
            Bucket=bucket,
            Key=variant_key(location_id, photo_index, size),
            Body=body,
            ContentType="image/webp",
            CacheControl=IMMUTABLE_CACHE_CONTROL,
        )
    client.put_object(
        Bucket=bucket,
        Key=original_key(location_id, photo_index, content_type),
        Body=image_bytes,
        ContentType=content_type or "image/jpeg",
        CacheControl=IMMUTABLE_CACHE_CONTROL,
    )
    logger.info(
        "r2: uploaded location=%s index=%d variants=%s original=%d bytes",
        location_id,
        photo_index,
        {size: len(body) for size, body in variants.items()},
        len(image_bytes),
    )
