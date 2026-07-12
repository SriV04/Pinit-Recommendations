"""
Magic AI-enrichment pre-warmer.

The Grok web-search agent routinely takes tens of seconds, so it never runs on
the user's request path — it runs in the background and populates the
``magic:ai_area`` cache for the *next* call. This module pre-warms that cache
for popular zones × intents on a schedule, so a real user almost always hits a
warm entry instead of triggering (and waiting a call later for) a cold refresh.

Run as a one-off:

    python -m pinit.cli.warm_magic --zones london

Or it is started automatically as a background loop from the API on startup
when ``MAGIC_PREWARM_ENABLED=true``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime
from typing import Awaitable, Callable, List, Sequence, Tuple
from zoneinfo import ZoneInfo

from pinit.api.services.cache_service import get_cache_service
from pinit.api.services.magic_ai_enrichment import build_magic_ai_enrichment_payload
from pinit.api.services.magic_ai_signature import build_magic_ai_signature
from pinit.api.services.magic_intent_parser import parse_magic_intent
from pinit.cli.warm_cache import (
    DEFAULT_WARM_TIMEZONE,
    DEFAULT_WARM_WINDOW_END_HOUR,
    DEFAULT_WARM_WINDOW_START_HOUR,
    HOT_ZONES,
    is_within_warm_cache_window,
)
from pinit.integrations.supabase import get_supabase_service

logger = logging.getLogger("pinit.warm_magic")

DEFAULT_MAGIC_PREWARM_INTERVAL_SECONDS = 900
# Cap Grok calls per pass so a cold matrix warms over several passes rather than
# firing dozens of slow web searches at once. Warm (24h TTL) keys are skipped,
# so steady-state passes do little or no work.
DEFAULT_MAGIC_PREWARM_MAX_REFRESHES = 8
# Short TTL for negative-cached (empty) results — mirrors the request path so a
# transient agent failure doesn't suppress enrichment for the full area TTL.
_MAGIC_AI_AREA_EMPTY_TTL_SECONDS = 600
# Keep in sync with proximal._MAGIC_AI_AREA_GRID_DEG so pre-warmed keys line up
# with the keys live "near me" searches compute from their result-median.
_MAGIC_AI_AREA_GRID_DEG = 0.1

# Popular generic intents to warm per zone. Cuisine-specific prompts would
# explode the matrix; these cover the common food_discovery / date / brunch /
# drinks / hidden-gem signatures.
MAGIC_PREWARM_PROMPTS: Tuple[str, ...] = (
    "restaurants near me",
    "new restaurants near me",
    "date night restaurants",
    "best brunch near me",
    "cocktail bars near me",
    "hidden gem restaurants",
)


def _snap(value: float) -> float:
    cell = _MAGIC_AI_AREA_GRID_DEG
    return round(round(value / cell) * cell, 2)


async def warm_magic_zone(
    *,
    lat: float,
    lng: float,
    prompt: str,
    radius_km: float = 2.0,
    cache: object | None = None,
    supabase: object | None = None,
) -> bool:
    """Pre-warm one (area, prompt) magic enrichment entry.

    Returns True if a refresh was performed (cold/empty key), False if the
    entry was already warm and skipped.
    """
    cache = cache or get_cache_service()
    supabase = supabase or get_supabase_service()

    intent = parse_magic_intent(prompt)
    signature = build_magic_ai_signature(
        intent=intent,
        raw_prompt=prompt,
        lat=_snap(lat),
        lng=_snap(lng),
        radius_km=radius_km,
    )
    key = cache.build_magic_ai_area_key(signature)

    existing = cache.get_magic_ai_area_results(key)
    if existing is not None and (existing.get("items") or []):
        return False  # already warm

    payload = await build_magic_ai_enrichment_payload(
        prompt=prompt,
        lat=_snap(lat),
        lng=_snap(lng),
        radius_km=radius_km,
        signature=signature,
        user_profile=None,
        supabase=supabase,
    )
    item_count = len(payload.get("items") or [])
    ttl_seconds = None if item_count else _MAGIC_AI_AREA_EMPTY_TTL_SECONDS
    cache.set_magic_ai_area_results(key, payload, ttl_seconds=ttl_seconds)
    logger.info(
        "Pre-warmed magic enrichment key=%s items=%d ttl=%s",
        key,
        item_count,
        ttl_seconds or "default",
    )
    return True


async def warm_magic_pass(
    *,
    zone_set: str = "london",
    prompts: Sequence[str] = MAGIC_PREWARM_PROMPTS,
    radius_km: float = 2.0,
    max_refreshes: int = DEFAULT_MAGIC_PREWARM_MAX_REFRESHES,
) -> int:
    """Run one pre-warm pass, refreshing at most ``max_refreshes`` cold keys.

    Returns the number of entries refreshed.
    """
    cache = get_cache_service()
    if not getattr(cache, "is_available", False):
        logger.warning("Redis cache not available; skipping magic pre-warm")
        return 0

    zones = HOT_ZONES.get(zone_set)
    if not zones:
        logger.error("Unknown zone set: %s (known: %s)", zone_set, list(HOT_ZONES))
        return 0

    supabase = get_supabase_service()
    refreshed = 0
    for name, lat, lng in zones:
        for prompt in prompts:
            if refreshed >= max_refreshes:
                logger.info(
                    "Magic pre-warm hit per-pass cap (%d); remaining cold keys "
                    "will warm next pass",
                    max_refreshes,
                )
                return refreshed
            try:
                did_refresh = await warm_magic_zone(
                    lat=lat,
                    lng=lng,
                    prompt=prompt,
                    radius_km=radius_km,
                    cache=cache,
                    supabase=supabase,
                )
                if did_refresh:
                    refreshed += 1
            except Exception as exc:
                logger.exception(
                    "Magic pre-warm failed for zone=%s prompt=%r: %s", name, prompt, exc
                )

    logger.info("Magic pre-warm pass refreshed %d entries (zone_set=%s)", refreshed, zone_set)
    return refreshed


async def warm_magic_loop(
    *,
    zone_set: str = "london",
    prompts: Sequence[str] = MAGIC_PREWARM_PROMPTS,
    radius_km: float = 2.0,
    interval_seconds: int = DEFAULT_MAGIC_PREWARM_INTERVAL_SECONDS,
    max_refreshes: int = DEFAULT_MAGIC_PREWARM_MAX_REFRESHES,
    start_hour: int = DEFAULT_WARM_WINDOW_START_HOUR,
    end_hour: int = DEFAULT_WARM_WINDOW_END_HOUR,
    timezone_name: str = DEFAULT_WARM_TIMEZONE,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now_fn: Callable[[], datetime] | None = None,
    max_passes: int | None = None,
) -> None:
    """Continuously pre-warm magic enrichment within the configured cost window."""
    passes = 0
    tz = ZoneInfo(timezone_name)

    while max_passes is None or passes < max_passes:
        now = now_fn() if now_fn is not None else datetime.now(tz)
        if is_within_warm_cache_window(now, start_hour=start_hour, end_hour=end_hour):
            try:
                await warm_magic_pass(
                    zone_set=zone_set,
                    prompts=prompts,
                    radius_km=radius_km,
                    max_refreshes=max_refreshes,
                )
            except Exception as exc:
                logger.exception("Magic pre-warm loop pass failed: %s", exc)
        else:
            logger.info(
                "Magic pre-warm skipped outside cost window (%02d:00-%02d:00 %s)",
                start_hour,
                end_hour,
                timezone_name,
            )

        passes += 1
        await sleep_fn(interval_seconds)


# ── env helpers (mirror warm_cache) ─────────────────────────────────────────

def magic_prewarm_enabled_from_env() -> bool:
    return os.getenv("MAGIC_PREWARM_ENABLED", "false").strip().lower() in {"1", "true", "yes"}


def magic_prewarm_zone_set_from_env() -> str:
    return os.getenv("MAGIC_PREWARM_ZONES", "london").strip() or "london"


def magic_prewarm_interval_from_env() -> int:
    raw = os.getenv("MAGIC_PREWARM_INTERVAL_SECONDS", "").strip()
    try:
        return max(60, int(raw)) if raw else DEFAULT_MAGIC_PREWARM_INTERVAL_SECONDS
    except ValueError:
        return DEFAULT_MAGIC_PREWARM_INTERVAL_SECONDS


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-warm magic AI enrichment cache.")
    parser.add_argument("--zones", type=str, default="london")
    parser.add_argument("--radius-km", type=float, default=2.0)
    parser.add_argument(
        "--max-refreshes", type=int, default=DEFAULT_MAGIC_PREWARM_MAX_REFRESHES
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    refreshed = asyncio.run(
        warm_magic_pass(
            zone_set=args.zones,
            radius_km=args.radius_km,
            max_refreshes=args.max_refreshes,
        )
    )
    return 0 if refreshed >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
