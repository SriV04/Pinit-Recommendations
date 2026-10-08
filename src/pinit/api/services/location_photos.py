"""The one photo step for a location: Google Places -> R2 (and Supabase
Storage while ``PHOTO_DUAL_WRITE_SUPABASE`` is on).

Every photo path goes through :func:`ensure_location_photos`:

* post-create processing stores the primary plus a couple of extras,
* ``/recommendations/proximal`` queues the same for returned places that have
  no photo yet (see :func:`schedule_missing_photos`),
* ``POST /locations/{id}/photos`` (a place tap) answers at once with stored
  CDN URLs plus short-lived Google ``photoUri`` links for the rest, and hands
  those links to this step so R2 has them for the next open.

Photos are stored contiguously (primary ``0``, extras ``1..n``), so
``image_stored`` and ``extra_photos_stored`` always describe a prefix. A Redis
lock per location keeps concurrent triggers from downloading twice.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import uuid4

import requests

from pinit.config import secrets
from pinit.integrations import r2_photos

logger = logging.getLogger(__name__)

MAX_PHOTOS = 10
# Photos stored ahead of any tap: enough for the first swipes, a third of the
# Google cost of a full gallery for places nobody opens.
PREFETCH_PHOTOS = 3
# Cap on background photo jobs one proximal response may queue.
MAX_BACKGROUND_PER_REQUEST = 10
LOCK_TTL_SECONDS = 300
PHOTO_URI_TIMEOUT_SECONDS = 4
PHOTO_NAMES_TIMEOUT_SECONDS = 5
# Photos stored for a place seen in a list: lists show only the first; the
# rest are stored when someone opens the place.
LIST_PHOTOS = 1

_local_locks: set[int] = set()
_local_locks_guard = threading.Lock()

# A photo to store: its Places resource name and how to get its bytes.
PhotoSource = Tuple[str, Callable[[], Optional[Tuple[bytes, str]]]]


# ─── Row helpers ─────────────────────────────────────────────────────────


def photo_names(row: Dict[str, Any]) -> List[str]:
    """Places v1 resource names from the row's ``photos`` jsonb, in order."""
    raw = row.get("photos")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    names: List[str] = []
    for photo in raw:
        name = photo.get("name") if isinstance(photo, dict) else None
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def stored_count(row: Dict[str, Any]) -> int:
    """Number of contiguous photos already stored (primary + extras)."""
    if row.get("image_stored") is not True:
        return 0
    try:
        extras = int(row.get("extra_photos_stored") or 0)
    except (TypeError, ValueError):
        extras = 0
    return 1 + max(extras, 0)


def needs_photo(row: Dict[str, Any]) -> bool:
    return row.get("image_stored") is not True and row.get("image_unavailable") is not True


def stored_photo_url(location_id: int, index: int) -> Optional[str]:
    """Public URL of a stored photo: the CDN hero variant when R2 serves
    photos, else the Supabase Storage object."""
    base = (secrets.PHOTO_CDN_BASE_URL or "").strip().rstrip("/")
    if base and r2_photos.is_configured():
        return f"{base}/{r2_photos.variant_key(location_id, index, 'hero')}"
    supabase_url = (secrets.SUPABASE_URL or "").strip().rstrip("/")
    if not supabase_url:
        return None
    name = f"{location_id}.jpg" if index == 0 else f"{location_id}_{index}.jpg"
    return f"{supabase_url}/storage/v1/object/public/location_photos/{name}"


# ─── Locking ─────────────────────────────────────────────────────────────


def _redis():
    try:
        from pinit.api.services.cache_service import get_cache_service

        cache = get_cache_service()
        if cache.is_available():
            return cache._redis_client
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("photo lock: redis unavailable (%s)", exc)
    return None


def _acquire(location_id: int) -> Optional[Callable[[], None]]:
    """Take the per-location photo lock; returns a release callable, or None
    when another worker holds it. Falls back to an in-process lock without
    Redis."""
    client = _redis()
    if client is not None:
        key = f"photos:lock:{int(location_id)}"
        token = str(uuid4())
        try:
            if not client.set(key, token, nx=True, ex=LOCK_TTL_SECONDS):
                return None

            def release() -> None:
                try:
                    if client.get(key) in (token, token.encode()):
                        client.delete(key)
                except Exception:
                    pass

            return release
        except Exception as exc:
            logger.warning("photo lock: redis error, using local lock (%s)", exc)

    with _local_locks_guard:
        if location_id in _local_locks:
            return None
        _local_locks.add(location_id)

    def release_local() -> None:
        with _local_locks_guard:
            _local_locks.discard(location_id)

    return release_local


# ─── Google ──────────────────────────────────────────────────────────────


class StalePhotoName(Exception):
    """Google rejected a cached photo name: Places photo names expire, so
    the place's Details must be fetched again for current ones."""


def google_photo_uri(name: str, max_px: Optional[int] = None) -> Optional[str]:
    """Short-lived ``photoUri`` for a Places photo (one billed Photo request).
    Downloading from the returned URI is a plain image fetch. Raises
    :class:`StalePhotoName` when the name has expired."""
    px = max_px or r2_photos.ingest_max_px()
    try:
        response = requests.get(
            f"https://places.googleapis.com/v1/{name}/media",
            params={
                "maxWidthPx": px,
                "maxHeightPx": px,
                "skipHttpRedirect": "true",
                "key": secrets.GOOGLE_PLACE_API_KEY,
            },
            timeout=PHOTO_URI_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("photoUri lookup failed for %s: %s", name, exc)
        return None
    if response.status_code == 400 and "INVALID_ARGUMENT" in response.text:
        raise StalePhotoName(name)
    try:
        response.raise_for_status()
        uri = response.json().get("photoUri")
        return uri if isinstance(uri, str) and uri else None
    except Exception as exc:
        logger.warning("photoUri lookup failed for %s: %s", name, exc)
        return None


def _fetch_uri(uri: str) -> Optional[Tuple[bytes, str]]:
    try:
        response = requests.get(uri, timeout=10)
        response.raise_for_status()
        return response.content, response.headers.get("Content-Type", "image/jpeg")
    except Exception as exc:
        logger.warning("photo download failed: %s", exc)
        return None


def _from_name(name: str) -> Callable[[], Optional[Tuple[bytes, str]]]:
    """Billed lookup of the photo's link, then a plain download. Raises
    :class:`StalePhotoName` when the name has expired."""

    def fetch() -> Optional[Tuple[bytes, str]]:
        uri = google_photo_uri(name)
        return _fetch_uri(uri) if uri else None

    return fetch


def _from_uri_or_name(
    name: str, uri: Optional[str]
) -> Callable[[], Optional[Tuple[bytes, str]]]:
    """Download from a paid-for ``photoUri`` when there is one; if it has
    expired (or there is none), make the billed request by name."""
    if not uri:
        return _from_name(name)

    def fetch() -> Optional[Tuple[bytes, str]]:
        return _fetch_uri(uri) or _from_name(name)()

    return fetch


# ─── The step ────────────────────────────────────────────────────────────


async def ensure_location_photos(
    location_id: int,
    want: int = PREFETCH_PHOTOS,
    *,
    supabase: Any = None,
    photo_uris: Optional[Sequence[Tuple[str, str]]] = None,
    photos: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Store photos for a location until ``want`` (capped at what Google has
    and :data:`MAX_PHOTOS`) are in place. Returns the stored count afterwards.

    ``photo_uris`` are ``(name, photoUri)`` pairs already fetched by the tap
    endpoint, in display order starting at the current stored count; they
    are downloaded instead of making new billed requests. ``photos`` is the
    Places Details ``photos`` array when the caller already has it (new
    places), so no extra Details call is made; an empty list means Google
    has no photos.
    """
    from pinit.integrations.supabase import get_supabase_service

    supabase = supabase or get_supabase_service()
    release = _acquire(location_id)
    if release is None:
        logger.info("photos: location %s already in progress", location_id)
        return -1
    try:
        return await _ensure_locked(location_id, want, supabase, photo_uris, photos)
    finally:
        release()


async def _ensure_locked(
    location_id: int,
    want: int,
    supabase: Any,
    photo_uris: Optional[Sequence[Tuple[str, str]]],
    known_photos: Optional[List[Dict[str, Any]]],
) -> int:
    row = await asyncio.to_thread(supabase.get_location, location_id)
    if not row or row.get("image_unavailable") is True:
        return 0

    have = stored_count(row)
    raw_photos = row.get("photos")
    names = photo_names(row)
    if not names and known_photos is not None:
        raw_photos = known_photos
        names = photo_names({"photos": known_photos})
    if not names and have == 0 and not photo_uris:
        if known_photos is None:
            details = await asyncio.to_thread(_refresh_photo_names, location_id, row, supabase)
            raw_photos = (details or {}).get("photos") or []
            names = photo_names({"photos": raw_photos})
        if not names:
            await asyncio.to_thread(supabase.mark_location_image_unavailable, location_id)
            return 0

    stored = have
    refreshed = False
    target = have
    while True:
        available = len(names) if names else have + len(photo_uris or ())
        target = min(max(want, 0), MAX_PHOTOS, max(available, have))
        if stored >= target:
            break

        uri_by_name = {name: uri for name, uri in photo_uris or ()}
        sources: List[PhotoSource] = []
        if names:
            # Match paid-for links by name: the job may run after another one
            # moved the stored prefix on.
            sources = [
                (name, _from_uri_or_name(name, uri_by_name.get(name)))
                for name in names[stored:target]
            ]
        elif photo_uris:
            sources = [(name, _from_uri_or_name(name, uri)) for name, uri in photo_uris]
        sources = sources[: target - stored]

        stored, stale = await _store_downloads(
            location_id, supabase, sources, stored, raw_photos
        )
        if not stale or refreshed or stored >= target:
            break

        # Google expired the cached photo names: fetch current ones once.
        refreshed = True
        details = await asyncio.to_thread(_refresh_photo_names, location_id, row, supabase)
        if details is None:
            break
        raw_photos = details.get("photos") or []
        names = photo_names({"photos": raw_photos})
        photo_uris = None
        if not names:
            if stored == 0:
                await asyncio.to_thread(supabase.mark_location_image_unavailable, location_id)
            break

    if stored > max(have, 1):
        await asyncio.to_thread(
            supabase.mark_location_extra_photos_stored, location_id, stored - 1
        )
    logger.info(
        "photos: location %s now has %d stored (was %d, wanted %d)",
        location_id,
        stored,
        have,
        target,
    )
    return stored


async def _store_downloads(
    location_id: int,
    supabase: Any,
    sources: List[PhotoSource],
    stored: int,
    raw_photos: Any,
) -> Tuple[int, bool]:
    """Download ``sources`` in parallel and store them from index ``stored``
    on, stopping at the first gap. Returns the new stored count and whether a
    photo name had expired."""
    downloads = await asyncio.gather(
        *(asyncio.to_thread(fetch) for _, fetch in sources),
        return_exceptions=True,
    )
    stale = False
    for (name, _), result in zip(sources, downloads):
        if isinstance(result, StalePhotoName):
            stale = True
            break
        if isinstance(result, BaseException):
            logger.warning("photos: download %s/%s failed: %s", location_id, stored, result)
            break
        if result is None:
            break  # keep the stored photos a contiguous prefix
        image_bytes, content_type = result
        index = None if stored == 0 else stored
        try:
            await asyncio.to_thread(
                supabase.upload_location_photo,
                location_id,
                image_bytes,
                content_type,
                index,
            )
        except Exception as exc:
            logger.error("photos: upload %s/%s failed: %s", location_id, stored, exc)
            break
        if stored == 0:
            await asyncio.to_thread(
                supabase.mark_location_image_uploaded,
                location_id,
                raw_photos if isinstance(raw_photos, list) else None,
                name,
            )
        stored += 1
    return stored, stale


def _refresh_photo_names(
    location_id: int, row: Dict[str, Any], supabase: Any
) -> Optional[Dict[str, Any]]:
    """Current photo names from Place Details, saved to the row so later
    steps match them. Requests only ``id,photos``, which Google bills as
    Place Details Essentials (IDs Only): free, with no cap. None when the
    place has no Place ID or the call fails."""
    place_id = str(row.get("google_place_id") or "").strip()
    if not place_id:
        return None
    try:
        response = requests.get(
            f"https://places.googleapis.com/v1/places/{place_id}",
            headers={
                "X-Goog-Api-Key": secrets.GOOGLE_PLACE_API_KEY,
                "X-Goog-FieldMask": "id,photos",
            },
            timeout=PHOTO_NAMES_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        photos = response.json().get("photos") or []
    except Exception as exc:
        logger.warning("photos: photo names refresh failed for %s: %s", location_id, exc)
        return None
    try:
        supabase.update_location(location_id, photos=photos)
    except Exception as exc:
        logger.warning("photos: could not save photo names for %s: %s", location_id, exc)
    return {"photos": photos}


# ─── Triggers ────────────────────────────────────────────────────────────
#
# Work that outlives a response goes to the Pub/Sub ``photos`` task, never to
# an in-process job: the API runs with CPU throttling, so anything left
# running after the response barely progresses and dies on scale-down.

Dispatch = Callable[[Any], Awaitable[None]]

# Cap on locations one ensure call accepts (one billed Photo request each
# for places with no stored photo).
MAX_ENSURE_IDS = 30


def photos_payload(
    location_id: int,
    want: int = PREFETCH_PHOTOS,
    *,
    google_place_id: str = "",
    photo_uris: Optional[Sequence[Tuple[str, str]]] = None,
    source: str = "photos",
):
    """A ``photos`` task for the location-task worker."""
    from pinit.api.schemas_location_tasks import PhotosPayload

    return PhotosPayload(
        task_type="photos",
        request_id=str(uuid4()),
        location_id=int(location_id),
        google_place_id=str(google_place_id or ""),
        source=source,
        want=min(max(int(want), 1), MAX_PHOTOS),
        photo_uris=[(name, uri) for name, uri in photo_uris or ()],
    )


async def _lookup_uris(names: Sequence[str]) -> Tuple[List[Optional[str]], bool]:
    """``photoUri`` per name (None where it failed), and whether any name
    had expired."""
    results = await asyncio.gather(
        *(asyncio.to_thread(google_photo_uri, name) for name in names),
        return_exceptions=True,
    )
    stale = any(isinstance(r, StalePhotoName) for r in results)
    return [r if isinstance(r, str) else None for r in results], stale


async def schedule_missing_photos(rows: Iterable[Dict[str, Any]], dispatch: Dispatch) -> int:
    """Queue the photo step for returned places with no photo yet, capped at
    :data:`MAX_BACKGROUND_PER_REQUEST`. Returns how many were queued."""
    queued = 0
    for row in rows:
        if queued >= MAX_BACKGROUND_PER_REQUEST:
            break
        if not needs_photo(row):
            continue
        try:
            location_id = int(row.get("location_id"))
        except (TypeError, ValueError):
            continue
        await dispatch(photos_payload(location_id, LIST_PHOTOS, source="proximal"))
        queued += 1
    return queued


async def gallery_for_tap(
    location_id: int,
    max_photos: int,
    *,
    supabase: Any,
    dispatch: Dispatch,
) -> Optional[List[str]]:
    """Ordered gallery for a place the user just opened: stored photos as
    public URLs, then Google ``photoUri`` links for the rest, which are
    queued for storage. None when the location does not exist."""
    row = await asyncio.to_thread(supabase.get_location, location_id)
    if not row:
        return None
    if row.get("image_unavailable") is True:
        return []

    cap = min(max(max_photos, 1), MAX_PHOTOS)
    have = min(stored_count(row), cap)
    urls = [u for u in (stored_photo_url(location_id, i) for i in range(have)) if u]
    place_id = str(row.get("google_place_id") or "")

    names = photo_names(row)
    if not names and have == 0:
        # No metadata yet. Someone is waiting on this screen, so make the
        # Details call now (the background step would make the same one).
        details = await asyncio.to_thread(_refresh_photo_names, location_id, row, supabase)
        if details is None:
            await dispatch(photos_payload(location_id, google_place_id=place_id, source="tap"))
            return urls
        names = photo_names({"photos": details.get("photos") or []})
        if not names:
            await asyncio.to_thread(supabase.mark_location_image_unavailable, location_id)
            return []

    missing = names[have:cap]
    if not missing:
        return urls

    uris, stale = await _lookup_uris(missing)
    if stale and uris[0] is None:
        # The cached names have expired; fetch current ones once.
        details = await asyncio.to_thread(_refresh_photo_names, location_id, row, supabase)
        if details is not None:
            names = photo_names({"photos": details.get("photos") or []})
            if not names and have == 0:
                await asyncio.to_thread(supabase.mark_location_image_unavailable, location_id)
                return []
            missing = names[have:cap]
            uris, _ = await _lookup_uris(missing)
    pairs: List[Tuple[str, str]] = []
    for name, uri in zip(missing, uris):
        if uri is None:
            break  # stay contiguous with what gets stored
        pairs.append((name, uri))

    if pairs:
        await dispatch(
            photos_payload(
                location_id,
                have + len(pairs),
                google_place_id=place_id,
                photo_uris=pairs,
                source="tap",
            )
        )
    return urls + [uri for _, uri in pairs]


async def ensure_primary_photos(
    location_ids: Sequence[int],
    *,
    supabase: Any,
    dispatch: Dispatch,
) -> Dict[int, str]:
    """Primary photo URL for each place a list is showing: the stored CDN
    URL, else a Google ``photoUri`` (one billed request) whose download is
    queued for storage. Places with no photo metadata are queued for the
    full step and left out; so are unknown and unavailable places."""
    ids = list(dict.fromkeys(int(i) for i in location_ids))[:MAX_ENSURE_IDS]
    if not ids:
        return {}
    rows = await asyncio.to_thread(supabase.get_locations_by_ids, ids)

    result: Dict[int, str] = {}
    to_link: List[Tuple[int, str, str]] = []  # (location_id, name, place_id)
    for row in rows or []:
        try:
            location_id = int(row.get("location_id"))
        except (TypeError, ValueError):
            continue
        if row.get("image_unavailable") is True:
            continue
        place_id = str(row.get("google_place_id") or "")
        if row.get("image_stored") is True:
            url = stored_photo_url(location_id, 0)
            if url:
                result[location_id] = url
            continue
        names = photo_names(row)
        if names:
            to_link.append((location_id, names[0], place_id))
        else:
            await dispatch(
                photos_payload(location_id, LIST_PHOTOS, google_place_id=place_id, source="list")
            )

    # An expired name gets no link here; the queued step refreshes Details.
    uris, _ = await _lookup_uris([name for _, name, _ in to_link])
    for (location_id, name, place_id), uri in zip(to_link, uris):
        pairs = [(name, uri)] if uri else None
        await dispatch(
            photos_payload(
                location_id, LIST_PHOTOS, google_place_id=place_id, photo_uris=pairs, source="list"
            )
        )
        if uri:
            result[location_id] = uri
    return result
