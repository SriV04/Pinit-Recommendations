#!/usr/bin/env python3
"""
Stage A – London Food-Venue Discovery Pipeline
===============================================

Cost-efficient, high-coverage pipeline that collects Google Places venue IDs
plus minimal metadata for ALL food venues in Greater London and upserts them
into the existing Supabase Postgres ``public.locations`` table.

Key properties
--------------
* Uses **Nearby Search (New)** with FieldMask – Pro-tier billing to capture
  rating, userRatingCount, and priceLevel alongside discovery.
* Adaptive quadtree tiling to handle the 20-result cap in dense areas.
* Subdivision depth limit (default 2) to cap API calls in dense areas.
* Fully resumable via a durable JSON state file.
* UPSERT mode: on conflict (google_place_id), UPDATES existing rows with
  Pro-tier fields while preserving enriched columns (editorial_summary,
  website, opening_hours, etc.).

Usage
-----
    python scripts/data_collection/stage_a_discover.py [OPTIONS]

    # Re-run over already-discovered tiles to backfill Pro fields:
    python scripts/data_collection/stage_a_discover.py --fresh

Required env vars (loaded from .env automatically):
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GOOGLE_MAPS_API_KEY

Run ``--help`` for full CLI options.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load environment
# ---------------------------------------------------------------------------
load_dotenv()

GOOGLE_MAPS_API_KEY: str = os.environ.get("GOOGLE_MAPS_API_KEY", "")
SUPABASE_URL: str = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY: str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FOOD_TYPES: List[str] = [
    "restaurant",
    "cafe",
    "bar",
    "bakery",
    "meal_takeaway",
    "night_club",
]

# Greater London bounding box
DEFAULT_LAT_MIN = 51.2868
DEFAULT_LAT_MAX = 51.6919
DEFAULT_LON_MIN = -0.5103
DEFAULT_LON_MAX = 0.3340

DEFAULT_INITIAL_RADIUS_M = 1400  # coarse starting radius (meters)
DEFAULT_MIN_RADIUS_M = 250       # stop subdividing below this
DEFAULT_MAX_SUBDIVISIONS = 0     # 0 = no subdivision (accept 20-result cap)
MAX_RESULT_COUNT = 20            # Nearby Search (New) hard cap per request

# Relaxed saturation threshold — accept missing a few venues in dense areas
# to significantly reduce subdivision (and therefore API calls).
# At 17/20, only tiles that are truly packed trigger subdivision.
SATURATION_THRESHOLD = 17

# Nearby Search (New) endpoint
NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"

# FieldMask — Enterprise-tier: includes all Pro fields plus websites,
# reviews, opening hours, vibe booleans, and serves booleans.
# Billing: $40/1K calls (vs $32 Pro, $5 Essentials).
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.types",
    "places.formattedAddress",
    "places.shortFormattedAddress",
    "places.location",
    "places.businessStatus",
    "places.googleMapsUri",
    "places.photos",
    # --- Pro-tier fields ---
    "places.rating",
    "places.userRatingCount",
    "places.priceLevel",
    "places.currentOpeningHours",
    # --- Enterprise-tier fields ---
    "places.reviews",
    "places.websiteUri",
    "places.goodForChildren",
    "places.goodForGroups",
    "places.goodForWatchingSports",
    "places.liveMusic",
    "places.outdoorSeating",
    "places.reviewSummary",
    "places.servesBeer",
    "places.servesBreakfast",
    "places.servesBrunch",
    "places.servesCocktails",
    "places.servesCoffee",
    "places.servesDessert",
    "places.servesDinner",
    "places.servesLunch",
    "places.servesVegetarianFood",
    "places.servesWine",
])

# Throttling / retry
REQUEST_SLEEP_S = 0.15
MAX_RETRIES = 6
RETRY_BACKOFF_BASE = 2.0
RETRY_MAX_WAIT = 30.0

# ---------------------------------------------------------------------------
# Pricing model (post-March 2025)
# ---------------------------------------------------------------------------

PRO_TIER_FIELDS = {
    "places.rating", "places.userRatingCount", "places.priceLevel",
    "places.regularOpeningHours", "places.currentOpeningHours",
    "places.primaryType", "places.primaryTypeDisplayName",
}

ENTERPRISE_TIER_FIELDS = {
    "places.reviews", "places.websiteUri", "places.internationalPhoneNumber",
    "places.nationalPhoneNumber", "places.editorialSummary", "places.reviewSummary",
    "places.goodForChildren", "places.goodForGroups", "places.goodForWatchingSports",
    "places.liveMusic", "places.outdoorSeating",
    "places.servesBeer", "places.servesBreakfast", "places.servesBrunch",
    "places.servesCocktails", "places.servesCoffee", "places.servesDessert",
    "places.servesDinner", "places.servesLunch", "places.servesVegetarianFood",
    "places.servesWine",
}

PRICING_CPM = {
    "essentials": 5.00,
    "pro":        32.00,
    "enterprise": 40.00,
}

DEFAULT_CREDIT_USD = 300.0


def detect_billing_tier(field_mask: str) -> str:
    fields = {f.strip() for f in field_mask.split(",")}
    if fields & ENTERPRISE_TIER_FIELDS:
        return "enterprise"
    if fields & PRO_TIER_FIELDS:
        return "pro"
    return "essentials"


# ---------------------------------------------------------------------------
# Price level mapping — New API returns enum strings, DB stores numeric
# ---------------------------------------------------------------------------

PRICE_LEVEL_MAP: Dict[str, int] = {
    "PRICE_LEVEL_FREE":            0,
    "PRICE_LEVEL_INEXPENSIVE":     1,
    "PRICE_LEVEL_MODERATE":        2,
    "PRICE_LEVEL_EXPENSIVE":       3,
    "PRICE_LEVEL_VERY_EXPENSIVE":  4,
}


class CostTracker:
    def __init__(self, tier: str, credit_usd: float = DEFAULT_CREDIT_USD):
        self.tier = tier
        self.cpm = PRICING_CPM[tier]
        self.credit_usd = credit_usd
        self.total_calls = 0

    def add_calls(self, n: int):
        self.total_calls += n

    @property
    def estimated_cost_usd(self) -> float:
        return self.total_calls * self.cpm / 1000.0

    @property
    def credit_remaining_usd(self) -> float:
        return max(0.0, self.credit_usd - self.estimated_cost_usd)

    @property
    def credit_used_pct(self) -> float:
        if self.credit_usd <= 0:
            return 100.0
        return min(100.0, (self.estimated_cost_usd / self.credit_usd) * 100)

    @property
    def calls_until_credit_exhausted(self) -> int:
        remaining = self.credit_remaining_usd
        if self.cpm <= 0:
            return 999_999
        return int(remaining / self.cpm * 1000)

    def summary(self) -> str:
        return (
            f"tier={self.tier}  calls={self.total_calls}  "
            f"cost=${self.estimated_cost_usd:.2f}  "
            f"credit_remaining=${self.credit_remaining_usd:.2f} "
            f"({self.credit_used_pct:.1f}% used)"
        )


UPSERT_BATCH_SIZE = 200

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("stage_a")

# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "pinit-stage-a/2.2",
    "Content-Type": "application/json",
    "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
    "X-Goog-FieldMask": FIELD_MASK,
})

# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def km_to_deg_lat(km: float) -> float:
    return km / 111.0


def km_to_deg_lon(km: float, lat_deg: float) -> float:
    return km / (111.0 * math.cos(math.radians(lat_deg)))


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6_371_000
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Tile data model
# ---------------------------------------------------------------------------

@dataclass
class Tile:
    lat: float
    lng: float
    radius_m: float
    depth: int = 0  # subdivision depth — 0 = root tile from coarse grid

    @property
    def key(self) -> str:
        return f"{round(self.lat, 5)}|{round(self.lng, 5)}|{int(self.radius_m)}"

    def to_dict(self) -> dict:
        return {"lat": self.lat, "lng": self.lng, "radius_m": self.radius_m, "depth": self.depth}

    @classmethod
    def from_dict(cls, d: dict) -> "Tile":
        # Backwards compat: old state files won't have 'depth'
        return cls(lat=d["lat"], lng=d["lng"], radius_m=d["radius_m"], depth=d.get("depth", 0))


def generate_coarse_grid(
    lat_min: float, lat_max: float, lon_min: float, lon_max: float,
    radius_m: float,
) -> List[Tile]:
    step_km = (radius_m / 1000.0) * 1.4
    dlat = km_to_deg_lat(step_km)
    mid_lat = (lat_min + lat_max) / 2.0
    dlon = km_to_deg_lon(step_km, mid_lat)

    tiles: List[Tile] = []
    lat = lat_min
    while lat <= lat_max + 1e-9:
        lng = lon_min
        while lng <= lon_max + 1e-9:
            tiles.append(Tile(lat=round(lat, 6), lng=round(lng, 6), radius_m=radius_m, depth=0))
            lng += dlon
        lat += dlat

    log.info("Grid generated: %d tiles (%.4f° lat step, %.4f° lon step, %dm radius)",
             len(tiles), dlat, dlon, radius_m)
    return tiles


def subdivide(tile: Tile) -> List[Tile]:
    """Split a tile into 4 children with half the radius and depth + 1."""
    new_r = tile.radius_m / 2.0
    new_depth = tile.depth + 1
    offset_km = new_r / 1000.0
    dlat = km_to_deg_lat(offset_km)
    dlng = km_to_deg_lon(offset_km, tile.lat)
    children: List[Tile] = []
    for lat_sign in (-1, 1):
        for lng_sign in (-1, 1):
            children.append(Tile(
                lat=round(tile.lat + lat_sign * dlat, 6),
                lng=round(tile.lng + lng_sign * dlng, 6),
                radius_m=new_r,
                depth=new_depth,
            ))
    return children


# ---------------------------------------------------------------------------
# State / resumability
# ---------------------------------------------------------------------------

@dataclass
class RunState:
    queue: List[dict] = field(default_factory=list)
    completed_keys: List[str] = field(default_factory=list)
    discovered_place_ids: int = 0
    api_calls: int = 0
    tiles_processed: int = 0
    tiles_subdivided: int = 0
    tiles_capped: int = 0  # tiles that hit subdivision depth limit
    rows_upserted: int = 0
    rows_errored: int = 0

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(asdict(self), f)
        tmp.replace(path)
        log.debug("State saved to %s (%d queued, %d completed keys)",
                  path, len(self.queue), len(self.completed_keys))

    @classmethod
    def load(cls, path: Path) -> "RunState":
        with open(path) as f:
            d = json.load(f)
        # Backwards compat: old state files may have different field names
        if "rows_inserted" in d:
            d["rows_upserted"] = d.pop("rows_inserted", 0)
        if "rows_skipped" in d:
            d["rows_errored"] = d.pop("rows_skipped", 0)
        if "tiles_capped" not in d:
            d["tiles_capped"] = 0
        state = cls(**d)
        log.info("State loaded from %s: %d queued tiles, %d completed keys, %d API calls so far",
                 path, len(state.queue), len(state.completed_keys), state.api_calls)
        return state


# ---------------------------------------------------------------------------
# Google Places (New) helpers
# ---------------------------------------------------------------------------

def _sleep_jitter(base: float):
    if base <= 0:
        return
    time.sleep(base * random.uniform(0.85, 1.15))


def nearby_search(lat: float, lng: float, radius_m: float, place_types: List[str]) -> Tuple[List[dict], int]:
    body = {
        "includedTypes": place_types,
        "maxResultCount": MAX_RESULT_COUNT,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(radius_m),
            }
        },
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.post(NEARBY_URL, json=body, timeout=30)
        except requests.RequestException as exc:
            wait = min(REQUEST_SLEEP_S * RETRY_BACKOFF_BASE ** (attempt - 1), RETRY_MAX_WAIT)
            log.warning("  ⚠ HTTP error on attempt %d/%d: %s — retrying in %.1fs",
                        attempt, MAX_RETRIES, exc, wait)
            _sleep_jitter(wait)
            continue

        if resp.status_code == 429:
            wait = min(5.0 * RETRY_BACKOFF_BASE ** (attempt - 1), RETRY_MAX_WAIT)
            log.warning("  ⚠ Rate limited (429) on attempt %d/%d — backing off %.1fs",
                        attempt, MAX_RETRIES, wait)
            _sleep_jitter(wait)
            continue

        if resp.status_code == 403:
            msg = resp.text[:200]
            raise RuntimeError(f"Google Places API 403 Forbidden: {msg}")

        if resp.status_code >= 500:
            wait = min(2.0 * RETRY_BACKOFF_BASE ** (attempt - 1), RETRY_MAX_WAIT)
            log.warning("  ⚠ Server error %d on attempt %d/%d — retrying in %.1fs",
                        resp.status_code, attempt, MAX_RETRIES, wait)
            _sleep_jitter(wait)
            continue

        if resp.status_code == 400:
            log.error("  ✗ Bad request (400): %s", resp.text[:300])
            return [], 1

        try:
            data = resp.json()
        except ValueError:
            log.warning("  ⚠ Invalid JSON on attempt %d/%d", attempt, MAX_RETRIES)
            _sleep_jitter(min(REQUEST_SLEEP_S * RETRY_BACKOFF_BASE ** (attempt - 1), RETRY_MAX_WAIT))
            continue

        places = data.get("places", [])
        return places, 1

    raise RuntimeError(f"nearby_search failed after {MAX_RETRIES} retries")


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------

def _build_location_row(place: dict, tile: Tile) -> Optional[dict]:
    """Convert a Nearby Search (New) result into a row dict for public.locations.

    Maps all Essentials + Pro + Enterprise fields from the API response
    to their corresponding DB columns. On conflict (existing google_place_id),
    Supabase will UPDATE only these columns — everything else is preserved.
    """
    place_id = place.get("id", "")
    display_name = place.get("displayName", {})
    name = (display_name.get("text") or "").strip()
    if not name or not place_id:
        return None

    location = place.get("location", {})
    types_list = place.get("types", [])
    types_str = ",".join(t.lower() for t in types_list) if types_list else None

    business_status = place.get("businessStatus")

    raw_price = place.get("priceLevel")
    price_level = PRICE_LEVEL_MAP.get(raw_price) if isinstance(raw_price, str) else raw_price

    # --- Opening hours ---
    current_hours = place.get("currentOpeningHours", {})
    opening_hours_text = current_hours.get("weekdayDescriptions")  # list of strings
    opening_hours_periods = current_hours.get("periods")           # list of period objects
    open_now = current_hours.get("openNow")

    # --- Photos (store resource names for later Photo API fetches) ---
    raw_photos = place.get("photos")
    photos_json = None
    if raw_photos:
        photos_json = [
            {"name": p.get("name"), "widthPx": p.get("widthPx"), "heightPx": p.get("heightPx")}
            for p in raw_photos if p.get("name")
        ]

    # --- Reviews (store raw for LLM tagging later) ---
    raw_reviews = place.get("reviews")
    reviews_json = None
    if raw_reviews:
        reviews_json = [
            {
                "rating": r.get("rating"),
                "text": r.get("text", {}).get("text", ""),
                "relativePublishTimeDescription": r.get("relativePublishTimeDescription", ""),
            }
            for r in raw_reviews
        ]

    # --- Review summary ---
    review_summary_obj = place.get("reviewSummary", {})
    review_summary = review_summary_obj.get("text", {}).get("text") if isinstance(review_summary_obj, dict) else None

    row: Dict[str, Any] = {
        "google_place_id": place_id,
        "name": name,
        "vicinity": place.get("shortFormattedAddress"),
        "lat": location.get("latitude"),
        "lng": location.get("longitude"),
        "types": types_str,
        "business_status": business_status,
        # --- Pro-tier ---
        "rating": place.get("rating"),
        "user_ratings_total": place.get("userRatingCount"),
        "price_level": price_level,
        # --- Opening hours ---
        "opening_hours_text": opening_hours_text,
        "opening_hours_periods": opening_hours_periods,
        "open_now": open_now,
        # --- Enterprise-tier ---
        "website": place.get("websiteUri"),
        "google_maps_uri": place.get("googleMapsUri"),
        "photos": photos_json,
        "reviews": reviews_json,
        "review_summary": review_summary,
        # --- Vibe / atmosphere booleans ---
        "good_for_children": place.get("goodForChildren"),
        "good_for_groups": place.get("goodForGroups"),
        "good_for_watching_sports": place.get("goodForWatchingSports"),
        "live_music": place.get("liveMusic"),
        "outdoor_seating": place.get("outdoorSeating"),
        # --- Serves booleans ---
        "serves_beer": place.get("servesBeer"),
        "serves_breakfast": place.get("servesBreakfast"),
        "serves_brunch": place.get("servesBrunch"),
        "serves_cocktails": place.get("servesCocktails"),
        "serves_coffee": place.get("servesCoffee"),
        "serves_dessert": place.get("servesDessert"),
        "serves_dinner": place.get("servesDinner"),
        "serves_lunch": place.get("servesLunch"),
        "serves_vegetarian_food": place.get("servesVegetarianFood"),
        "serves_wine": place.get("servesWine"),
        # --- Meta ---
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "data_version": "v2.2",
        "derived_attributes": {
            "stage": "A",
            "source": "nearby_new",
            "tier": "enterprise",
            "tile_radius_m": tile.radius_m,
            "tile_depth": tile.depth,
            "tile_center": {"lat": tile.lat, "lng": tile.lng},
            "tile_id": tile.key,
            "queried_types": FOOD_TYPES,
        },
    }
    return row


# ---------------------------------------------------------------------------
# Supabase upsert logic
# ---------------------------------------------------------------------------

class SupabaseUpserter:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._client = None
        self.upserted = 0
        self.errored = 0

    @property
    def client(self):
        if self._client is None:
            from supabase import create_client
            url = SUPABASE_URL.strip().rstrip("/") + "/"
            self._client = create_client(url, SUPABASE_SERVICE_ROLE_KEY.strip())
            log.info("Supabase client initialised (url=%s…)", url[:40])
        return self._client

    def upsert_batch(self, rows: List[dict]):
        if not rows:
            return

        if self.dry_run:
            log.info("  [DRY RUN] Would upsert %d rows", len(rows))
            return

        seen: dict = {}
        for row in rows:
            seen[row["google_place_id"]] = row
        unique_rows: List[dict] = list(seen.values())
        dupes_in_batch = len(rows) - len(unique_rows)
        if dupes_in_batch > 0:
            log.debug("  Deduplicated %d intra-batch duplicates", dupes_in_batch)

        for i in range(0, len(unique_rows), 50):
            chunk = unique_rows[i:i + 50]
            try:
                result = (
                    self.client
                    .table("locations")
                    .upsert(chunk, on_conflict="google_place_id")
                    .execute()
                )
                upserted_count = len(result.data) if result.data else 0
                self.upserted += upserted_count
                errored_count = len(chunk) - upserted_count
                self.errored += errored_count
                if upserted_count > 0:
                    log.debug("  Upserted chunk: %d rows written", upserted_count)
            except Exception as exc:
                log.warning("  ⚠ Batch upsert error: %s — falling back to row-by-row", exc)
                for row in chunk:
                    try:
                        r = (
                            self.client
                            .table("locations")
                            .upsert([row], on_conflict="google_place_id")
                            .execute()
                        )
                        if r.data:
                            self.upserted += 1
                        else:
                            self.errored += 1
                    except Exception as row_exc:
                        log.debug("  Row-level error for %s: %s",
                                  row.get("google_place_id", "?"), row_exc)
                        self.errored += 1


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(args: argparse.Namespace):
    max_subdivisions = args.max_subdivisions

    log.info("=" * 60)
    log.info("  Stage A – London Food-Venue Discovery Pipeline v2.2")
    log.info("  API: Nearby Search (New) with FieldMask (Enterprise tier)")
    log.info("  Mode: UPSERT (update existing rows with all fields)")
    log.info("  Types: %s", ", ".join(FOOD_TYPES))
    log.info("  Field mask: %s", FIELD_MASK)
    log.info("  Saturation threshold: %d/%d", SATURATION_THRESHOLD, MAX_RESULT_COUNT)
    log.info("  Max subdivision depth: %d (root=1400m → depth 1=700m → depth 2=350m)",
             max_subdivisions)

    billing_tier = detect_billing_tier(FIELD_MASK)
    cpm = PRICING_CPM[billing_tier]
    log.info("  Billing tier: %s ($%.2f/1K calls)", billing_tier.upper(), cpm)
    log.info("  Credit pool: $%.2f", args.credit)
    log.info("  Max calls before credit exhausted: %d",
             int(args.credit / cpm * 1000))
    log.info("=" * 60)

    if not GOOGLE_MAPS_API_KEY:
        log.error("✗ GOOGLE_MAPS_API_KEY not set. Aborting.")
        sys.exit(1)
    else:
        log.info("✓ GOOGLE_MAPS_API_KEY set (%s…)", GOOGLE_MAPS_API_KEY[:8])

    if not args.dry_run and (not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY):
        log.error("✗ SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set. Aborting.")
        sys.exit(1)
    elif not args.dry_run:
        log.info("✓ Supabase credentials set (url=%s…)", SUPABASE_URL[:30])

    state_path = Path(args.state_file)
    min_radius = args.min_radius

    if state_path.exists() and not args.fresh:
        log.info("Resuming from state file: %s", state_path)
        state = RunState.load(state_path)
        completed_set: Set[str] = set(state.completed_keys)
        queue: deque[Tile] = deque(Tile.from_dict(d) for d in state.queue)
    else:
        log.info("Fresh start — generating coarse grid over bbox "
                 "[%.4f,%.4f] × [%.4f,%.4f]",
                 args.lat_min, args.lat_max, args.lon_min, args.lon_max)
        tiles = generate_coarse_grid(
            args.lat_min, args.lat_max, args.lon_min, args.lon_max,
            args.initial_radius,
        )
        queue = deque(tiles)
        completed_set = set()
        state = RunState(queue=[t.to_dict() for t in tiles])
        log.info("Initial queue: %d coarse tiles (radius=%dm)", len(tiles), args.initial_radius)

    upserter = SupabaseUpserter(dry_run=args.dry_run)
    if args.dry_run:
        log.info("🔒 DRY RUN mode — no database writes will occur")

    cost = CostTracker(tier=billing_tier, credit_usd=args.credit)
    if state.api_calls > 0:
        cost.add_calls(state.api_calls)
        log.info("Restored cost tracker from state: %s", cost.summary())

    all_discovered: Set[str] = set()
    buffer: List[dict] = []

    total_api_calls = state.api_calls
    tiles_processed = state.tiles_processed
    tiles_subdivided = state.tiles_subdivided
    tiles_capped = state.tiles_capped
    max_requests = args.max_requests

    def flush_buffer():
        nonlocal buffer
        if buffer:
            log.info("  Flushing buffer: %d rows to upsert", len(buffer))
            upserter.upsert_batch(buffer)
            buffer = []

    def save_state():
        state.queue = [t.to_dict() for t in queue]
        state.completed_keys = list(completed_set)
        state.discovered_place_ids = len(all_discovered)
        state.api_calls = total_api_calls
        state.tiles_processed = tiles_processed
        state.tiles_subdivided = tiles_subdivided
        state.tiles_capped = tiles_capped
        state.rows_upserted = upserter.upserted
        state.rows_errored = upserter.errored
        state.save(state_path)

    initial_queue_size = len(queue)
    log.info("Strategy: 1 combined-type call per tile (%s)", "+".join(FOOD_TYPES))
    log.info("Estimated initial API calls (no subdivision): %d", initial_queue_size)
    est_min_cost = initial_queue_size * cpm / 1000
    est_max_calls = initial_queue_size * 3
    est_max_cost = est_max_calls * cpm / 1000
    log.info("Estimated cost range: $%.2f – $%.2f (of $%.2f credit)",
             est_min_cost, est_max_cost, args.credit)
    log.info("Starting discovery loop — %d tiles in queue …", len(queue))

    start_time = time.time()
    save_interval = 20
    log_interval = 10

    try:
        while queue:
            tile = queue.popleft()

            if tile.key in completed_set:
                log.debug("Skipping tile %s (already completed)", tile.key)
                continue

            if max_requests and total_api_calls >= max_requests:
                log.warning("⏸ Reached --max-requests=%d. Stopping.", max_requests)
                queue.appendleft(tile)
                raise KeyboardInterrupt

            tile_start = time.time()

            try:
                results, calls = nearby_search(
                    tile.lat, tile.lng, tile.radius_m, FOOD_TYPES,
                )
            except RuntimeError as exc:
                log.error("  ✗ Fatal API error for tile %s: %s — skipping", tile.key, exc)
                tiles_processed += 1
                continue

            total_api_calls += calls
            cost.add_calls(calls)
            result_count = len(results)

            # Subdivision allowed only if all three conditions met:
            #   1. Hit saturation threshold
            #   2. Radius above minimum
            #   3. Depth below the max subdivision limit
            can_subdivide = (
                result_count >= SATURATION_THRESHOLD
                and tile.radius_m > min_radius
                and tile.depth < max_subdivisions
            )

            # Detect tiles that WOULD subdivide but are depth-capped
            at_depth_limit = (
                result_count >= SATURATION_THRESHOLD
                and tile.radius_m > min_radius
                and tile.depth >= max_subdivisions
            )

            if can_subdivide:
                log.info("  🔴 SATURATED: tile %s → %d results (r=%dm, depth=%d) — subdividing",
                         tile.key, result_count, int(tile.radius_m), tile.depth)
            elif at_depth_limit:
                log.info("  🟡 CAPPED: tile %s → %d results (r=%dm, depth=%d/%d) — depth limit, accepting as-is",
                         tile.key, result_count, int(tile.radius_m), tile.depth, max_subdivisions)
            elif result_count > 0:
                log.debug("  ✓ tile %s → %2d results (r=%dm, depth=%d)",
                           tile.key, result_count, int(tile.radius_m), tile.depth)
            else:
                log.debug("  · tile %s → 0 results", tile.key)

            # ---- Build rows from results ----
            tile_new_places = 0
            for place in results:
                pid = place.get("id", "")
                if not pid:
                    continue
                is_new = pid not in all_discovered
                all_discovered.add(pid)
                if is_new:
                    tile_new_places += 1
                row = _build_location_row(place, tile)
                if row:
                    buffer.append(row)

            # ---- Handle subdivision vs completion ----
            if can_subdivide:
                children = subdivide(tile)
                queue.extend(children)
                tiles_subdivided += 1
                log.info("  ✂ Subdivided (r=%dm, depth=%d) → 4 children (r=%dm, depth=%d) — queue now %d",
                         int(tile.radius_m), tile.depth,
                         int(tile.radius_m / 2), tile.depth + 1, len(queue))
            else:
                if at_depth_limit:
                    tiles_capped += 1
                completed_set.add(tile.key)
                tiles_processed += 1

            tile_elapsed = time.time() - tile_start
            if tile_new_places > 0:
                log.debug("  Tile done: %d new places, %d total results (%.1fs)",
                          tile_new_places, result_count, tile_elapsed)

            if len(buffer) >= UPSERT_BATCH_SIZE:
                flush_buffer()

            if tiles_processed > 0 and tiles_processed % save_interval == 0:
                save_state()

            if tiles_processed > 0 and tiles_processed % log_interval == 0:
                elapsed = time.time() - start_time
                rate = total_api_calls / elapsed if elapsed > 0 else 0
                log.info(
                    "── Progress ─────────────────────────────────────────────────")
                log.info(
                    "  tiles: %d done, %d subdivided, %d depth-capped | queue: %d",
                    tiles_processed, tiles_subdivided, tiles_capped, len(queue))
                log.info(
                    "  api calls: %d (%.1f/s) | discovered: %d unique places",
                    total_api_calls, rate, len(all_discovered))
                log.info(
                    "  db: %d upserted, %d errors | elapsed: %.0fs",
                    upserter.upserted, upserter.errored, elapsed)
                log.info(
                    "  💰 cost: %s", cost.summary())
                log.info(
                    "─────────────────────────────────────────────────────────────")

    except KeyboardInterrupt:
        log.warning("⏸ Interrupted — saving state …")
    finally:
        flush_buffer()
        save_state()

    elapsed = time.time() - start_time
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║     Stage A – Post-Run Report (v2.2 Enterprise Tier)     ║")
    print("╠══════════════════════════════════════════════════════════╣")
    print(f"║  Tiles processed      : {tiles_processed:<28d} ║")
    print(f"║  Tiles subdivided     : {tiles_subdivided:<28d} ║")
    print(f"║  Tiles depth-capped   : {tiles_capped:<28d} ║")
    print(f"║  Max subdiv depth     : {max_subdivisions:<28d} ║")
    print(f"║  Saturation threshold : {SATURATION_THRESHOLD:<28d} ║")
    print(f"║  API calls            : {total_api_calls:<28d} ║")
    print(f"║  Unique place_ids     : {len(all_discovered):<28d} ║")
    print(f"║  Rows upserted        : {upserter.upserted:<28d} ║")
    print(f"║  Rows errored         : {upserter.errored:<28d} ║")
    print(f"║  Queue remaining      : {len(queue):<28d} ║")
    print(f"║  Elapsed              : {elapsed:>25.1f}s ║")
    print(f"║  Avg call rate        : {total_api_calls / max(elapsed, 1):>22.1f}/s ║")
    print(f"║  State file           : {str(state_path):<28s} ║")
    print("╠══════════════════════════════════════════════════════════╣")
    print(f"║  Billing tier         : {billing_tier.upper():<28s} ║")
    print(f"║  Rate (per 1K calls)  : ${cpm:<26.2f} ║")
    print(f"║  ESTIMATED COST       : ${cost.estimated_cost_usd:<26.2f} ║")
    print(f"║  Credit pool          : ${cost.credit_usd:<26.2f} ║")
    print(f"║  Credit remaining     : ${cost.credit_remaining_usd:<26.2f} ║")
    print(f"║  Credit used          : {cost.credit_used_pct:<27.1f}% ║")
    print(f"║  Calls until exhausted: {cost.calls_until_credit_exhausted:<28,d} ║")
    print("╠══════════════════════════════════════════════════════════╣")
    print("║  ⚠ Cost is estimated. Verify at:                       ║")
    print("║  console.cloud.google.com/billing                      ║")
    print("║  Prices: developers.google.com/maps/billing-and-       ║")
    print("║          pricing/pricing                                ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()
    log.info("Done. State saved to %s", state_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Stage A: London food-venue discovery via Google Places Nearby Search (New)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    bbox = p.add_argument_group("Bounding box")
    bbox.add_argument("--lat-min", type=float, default=DEFAULT_LAT_MIN)
    bbox.add_argument("--lat-max", type=float, default=DEFAULT_LAT_MAX)
    bbox.add_argument("--lon-min", type=float, default=DEFAULT_LON_MIN)
    bbox.add_argument("--lon-max", type=float, default=DEFAULT_LON_MAX)

    grid = p.add_argument_group("Grid / tiling")
    grid.add_argument("--initial-radius", type=int, default=DEFAULT_INITIAL_RADIUS_M,
                      help="Starting tile radius in meters (default: %(default)s)")
    grid.add_argument("--min-radius", type=int, default=DEFAULT_MIN_RADIUS_M,
                      help="Stop subdividing below this radius (default: %(default)s)")
    grid.add_argument("--max-subdivisions", type=int, default=DEFAULT_MAX_SUBDIVISIONS,
                      help="Max subdivision depth per tile — 0 disables subdivision entirely "
                           "(default: %(default)s → root 1400m → 700m → 350m)")

    run = p.add_argument_group("Run control")
    run.add_argument("--max-requests", type=int, default=0,
                     help="Stop after N API calls (0 = unlimited)")
    run.add_argument("--dry-run", action="store_true",
                     help="Fetch from Google but do NOT write to Supabase")
    run.add_argument("--fresh", action="store_true",
                     help="Ignore existing state file and start fresh")
    run.add_argument("--state-file", type=str,
                     default="data/raw/stage_a_state.json",
                     help="Path to resumable state file (default: %(default)s)")
    run.add_argument("--credit", type=float, default=DEFAULT_CREDIT_USD,
                     help="Google Cloud credit pool in USD (default: %(default)s)")
    run.add_argument("--verbose", "-v", action="store_true",
                     help="Enable DEBUG-level logging for granular output")

    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.verbose:
        logging.getLogger("stage_a").setLevel(logging.DEBUG)
        log.info("Verbose mode enabled (DEBUG logging)")
    run_pipeline(args)