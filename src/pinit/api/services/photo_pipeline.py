"""
Photo pipeline with waterfall sourcing: og:image → Google Places fallback.

The og:image meta tag from a venue's own website is tried first (free,
typically professional marketing photos).  Only when that fails do we
fall back to the existing Google Places photo pipeline (which also runs
OpenAI quality classification).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin

import requests

from pinit.api.services.proximal_service import (
    classify_image_with_openai,
    download_photo,
    get_photo_reference,
)

logger = logging.getLogger(__name__)

# Domains whose og:image is an ordering-platform logo, not a venue photo
_SKIP_OG_DOMAINS = {"ubereats.com", "doordash.com", "grubhub.com", "postmates.com", "seamless.com"}

_MIN_IMAGE_BYTES = 10_000  # 10 KB – smaller is likely a favicon or placeholder


@dataclass
class PhotoResult:
    image_bytes: bytes
    content_type: str
    source: str  # "og_image" | "google"
    source_reference: str  # URL or Google resource name
    score: Optional[int] = field(default=None)


# ---------------------------------------------------------------------------
# og:image parser
# ---------------------------------------------------------------------------

class _OGImageParser(HTMLParser):
    """Minimal HTML parser that extracts the og:image content attribute."""

    def __init__(self) -> None:
        super().__init__()
        self.og_image_url: Optional[str] = None
        self._in_head = False
        self._done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if self._done:
            return
        if tag == "head":
            self._in_head = True
            return
        if tag == "meta":
            attrs_dict = {k.lower(): v for k, v in attrs}
            prop = attrs_dict.get("property", "")
            if prop.lower() == "og:image" and attrs_dict.get("content"):
                self.og_image_url = attrs_dict["content"]
                self._done = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self._done = True


def _extract_og_image(html: str, base_url: str) -> Optional[str]:
    """Return the absolute og:image URL from *html*, or None."""
    parser = _OGImageParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    url = parser.og_image_url
    if not url:
        return None
    # Resolve relative URLs
    if not url.startswith(("http://", "https://")):
        url = urljoin(base_url, url)
    return url


def _is_skip_domain(url: str) -> bool:
    """Return True if *url* points to an ordering-platform domain."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        return any(host == d or host.endswith("." + d) for d in _SKIP_OG_DOMAINS)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Source: og:image
# ---------------------------------------------------------------------------

def _try_og_image(website_url: str) -> Optional[PhotoResult]:
    """Scrape the og:image meta tag from *website_url* and download the image."""
    if _is_skip_domain(website_url):
        logger.debug("Skipping og:image for ordering-platform URL: %s", website_url)
        return None

    try:
        resp = requests.get(
            website_url,
            timeout=5,
            headers={"User-Agent": "PinIt/1.0 (photo-pipeline)"},
            allow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.debug("og:image: failed to fetch website %s: %s", website_url, exc)
        return None

    og_url = _extract_og_image(resp.text, website_url)
    if not og_url:
        logger.debug("og:image: no og:image tag found on %s", website_url)
        return None

    # Download the actual image
    try:
        img_resp = requests.get(og_url, timeout=5, allow_redirects=True)
        img_resp.raise_for_status()
    except Exception as exc:
        logger.debug("og:image: failed to download image %s: %s", og_url, exc)
        return None

    content_type = img_resp.headers.get("Content-Type", "")
    if not content_type.startswith("image/"):
        logger.debug("og:image: non-image Content-Type %s for %s", content_type, og_url)
        return None

    image_bytes = img_resp.content
    if len(image_bytes) < _MIN_IMAGE_BYTES:
        logger.debug("og:image: image too small (%d bytes) from %s", len(image_bytes), og_url)
        return None

    logger.info("og:image: got %d-byte image from %s", len(image_bytes), og_url)
    return PhotoResult(
        image_bytes=image_bytes,
        content_type=content_type,
        source="og_image",
        source_reference=og_url,
        score=3,  # business-curated, skip OpenAI classification
    )


# ---------------------------------------------------------------------------
# Source: Google Places (existing pipeline)
# ---------------------------------------------------------------------------

def _try_google(google_place_id: str, api_key: str) -> Optional[PhotoResult]:
    """Fetch and classify a photo via the existing Google Places pipeline."""
    photo_ref = get_photo_reference(google_place_id, api_key)
    if not photo_ref:
        return None

    download_result = download_photo(photo_ref, api_key)
    if not download_result:
        return None

    image_bytes, content_type = download_result
    score = classify_image_with_openai(image_bytes)

    return PhotoResult(
        image_bytes=image_bytes,
        content_type=content_type,
        source="google",
        source_reference=photo_ref,
        score=score,
    )


# ---------------------------------------------------------------------------
# Waterfall orchestrator
# ---------------------------------------------------------------------------

def fetch_photo_waterfall(
    location_row: dict,
    google_api_key: str,
) -> Optional[PhotoResult]:
    """Try photo sources in priority order; return the first success.

    *location_row* must contain at least ``website`` (may be None) and
    ``google_place_id``.
    """
    website = location_row.get("website")
    if website:
        result = _try_og_image(website)
        if result:
            return result

    google_place_id = location_row.get("google_place_id")
    if google_place_id:
        result = _try_google(google_place_id, google_api_key)
        if result:
            return result

    return None
