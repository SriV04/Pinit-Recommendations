"""
Cache pre-warmer.

Runs two jobs:

  1. Refresh the global LPA snapshot in Redis
       (entire `location_popularity_app` joined to slim location columns).

  2. Pre-warm proximal cache entries for a set of hot zones (London by
     default) so live user requests always hit a warm cache and never pay
     the SQL miss cost.

Intended as a cron entrypoint:

    */5 * * * *  python -m pinit.cli.warm_cache --zones london

Or as a one-off:

    python -m pinit.cli.warm_cache --refresh-snapshot
    python -m pinit.cli.warm_cache --zones london --radius-km 15

Exit codes: 0 on success, 1 if any job failed (snapshot or zone fetch).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Dict, List, Tuple

from pinit.api.services.cache_service import get_cache_service
from pinit.integrations.supabase import get_supabase_service


logger = logging.getLogger("pinit.warm_cache")


# Hot-zone catalogue. Centre coords for the proximal cache key, plus a
# radius wide enough that the cached payload covers a fair walk in any
# direction. Add zones as new markets come online.
HOT_ZONES: Dict[str, List[Tuple[str, float, float]]] = {
    "london": [
        ("notting_hill",   51.5145, -0.2065),
        ("soho",           51.5137, -0.1310),
        ("shoreditch",     51.5260, -0.0786),
        ("covent_garden",  51.5117, -0.1240),
        ("mayfair",        51.5095, -0.1490),
        ("kings_cross",    51.5320, -0.1233),
        ("camden",         51.5390, -0.1426),
        ("brixton",        51.4626, -0.1145),
        ("clapham",        51.4625, -0.1380),
        ("canary_wharf",   51.5054, -0.0235),
        ("hackney",        51.5450, -0.0610),
    ],
}


def refresh_lpa_snapshot() -> bool:
    """Pull the full LPA + slim locations join, write to Redis."""
    cache = get_cache_service()
    supabase = get_supabase_service()

    if not cache.is_available:
        logger.error("Redis cache not available; skipping LPA snapshot refresh")
        return False

    started = time.time()
    rows = supabase.fetch_lpa_snapshot_rows()
    if not rows:
        logger.warning("LPA snapshot fetch returned 0 rows; nothing to write")
        return False

    ok = cache.set_lpa_snapshot(rows)
    elapsed = time.time() - started
    logger.info(
        "LPA snapshot refresh: %d rows in %.2fs (success=%s)",
        len(rows),
        elapsed,
        ok,
    )
    return ok


def warm_zones(zone_set: str, radius_km: float = 15.0) -> int:
    """
    Hit the SQL fetch path for each hot zone so its 15 km cache entry is
    live in Redis. Returns the number of zones successfully warmed.
    """
    cache = get_cache_service()
    supabase = get_supabase_service()
    if not cache.is_available:
        logger.error("Redis cache not available; skipping zone warm-up")
        return 0

    zones = HOT_ZONES.get(zone_set)
    if not zones:
        logger.error("Unknown zone set: %s (known: %s)", zone_set, list(HOT_ZONES))
        return 0

    successes = 0
    for name, lat, lng in zones:
        try:
            started = time.time()
            # Same combined path the request handler uses on cache miss:
            # snapshot for primary_set, fill RPC for the rest.
            snapshot = cache.get_lpa_snapshot()
            if snapshot:
                primary = cache.filter_lpa_snapshot_by_radius(snapshot, lat, lng, radius_km)
                fill = supabase.get_fill_locations(
                    lat, lng, radius_km, limit=max(1000 - len(primary), 0)
                )
                candidates = primary + fill
            else:
                candidates = supabase.get_locations_with_quality_scores(
                    lat, lng, radius_km, limit=1000
                )

            if not candidates:
                logger.warning("Zone %s: 0 candidates", name)
                continue

            cache.set_cached_recommendations(lat, lng, candidates)
            elapsed = time.time() - started
            logger.info(
                "Zone %-18s @ (%.4f, %.4f): %d candidates cached in %.2fs",
                name,
                lat,
                lng,
                len(candidates),
                elapsed,
            )
            successes += 1
        except Exception as exc:
            logger.exception("Zone %s warm-up failed: %s", name, exc)

    logger.info("Warmed %d/%d zones in set '%s'", successes, len(zones), zone_set)
    return successes


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-warm the proximal cache.")
    parser.add_argument(
        "--refresh-snapshot",
        action="store_true",
        help="Refresh the global LPA snapshot in Redis.",
    )
    parser.add_argument(
        "--zones",
        type=str,
        default=None,
        help=f"Hot-zone set to warm (one of: {', '.join(HOT_ZONES)}).",
    )
    parser.add_argument(
        "--radius-km",
        type=float,
        default=15.0,
        help="Cache radius in km for zone warm-up (default 15.0).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run both snapshot refresh and zone warm-up (default zone set: london).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Default behaviour when invoked with no flags: do everything for london.
    if not (args.refresh_snapshot or args.zones or args.all):
        args.all = True

    failures = 0
    if args.refresh_snapshot or args.all:
        if not refresh_lpa_snapshot():
            failures += 1

    if args.zones or args.all:
        zone_set = args.zones or "london"
        warmed = warm_zones(zone_set, radius_km=args.radius_km)
        if warmed == 0:
            failures += 1

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
