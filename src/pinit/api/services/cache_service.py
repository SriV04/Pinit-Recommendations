"""
Redis-based caching service for proximal recommendations.

Implements a two-tier caching strategy:
1. Tier 1: Large-radius (15km) unfiltered recommendations with component scores
2. Tier 2: User taste score pre-computation (Phase 2)

The cache allows a single entry to serve multiple request variations by:
- Storing large radius results and filtering in-memory
- Storing component scores (taste, proximity, quality) separately
- Supporting re-ranking with different weights without server calls
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
import redis
from redis.exceptions import RedisError

from pinit.config.settings import CacheConfig

logger = logging.getLogger(__name__)


class ProximalCacheService:
    """Redis-based cache service for proximal recommendations."""

    def __init__(self, config: Optional[CacheConfig] = None):
        """
        Initialize cache service with Redis connection.

        Args:
            config: Cache configuration. If None, uses defaults from environment.
        """
        self.config = config or CacheConfig()
        self._redis_client: Optional[redis.Redis] = None

        # Initialize Redis connection if caching is enabled
        if self.config.caching_enabled:
            self._connect_redis()

    def _connect_redis(self) -> None:
        """Establish Redis connection."""
        try:
            connection_params = {
                "host": self.config.redis_host,
                "port": self.config.redis_port,
                "db": self.config.redis_db,
                "password": self.config.redis_password,
                "decode_responses": False,  # We'll handle encoding ourselves
                "socket_connect_timeout": 5,
                "socket_timeout": 5,
            }

            # Add SSL/TLS support for managed Redis services (e.g., Upstash, Redis Cloud)
            if self.config.redis_ssl:
                connection_params["ssl"] = True
                connection_params["ssl_cert_reqs"] = None  # Don't verify certificates (for managed services)
                logger.debug("Redis SSL/TLS enabled")

            self._redis_client = redis.Redis(**connection_params)

            # Test connection
            self._redis_client.ping()
            logger.info(
                "Connected to Redis at %s:%d (db=%d, ssl=%s)",
                self.config.redis_host,
                self.config.redis_port,
                self.config.redis_db,
                self.config.redis_ssl,
            )
        except RedisError as exc:
            logger.error("Failed to connect to Redis: %s. Caching disabled.", exc)
            self._redis_client = None
        except Exception as exc:
            logger.error("Unexpected error connecting to Redis: %s. Caching disabled.", exc)
            self._redis_client = None

    @property
    def is_available(self) -> bool:
        """Check if cache is available."""
        if not self.config.caching_enabled:
            return False
        if self._redis_client is None:
            return False
        try:
            self._redis_client.ping()
            return True
        except RedisError:
            return False

    def _haversine_distance(self, lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        """
        Calculate distance between two points using Haversine formula.

        Args:
            lat1, lng1: First point coordinates
            lat2, lng2: Second point coordinates

        Returns:
            Distance in kilometers
        """
        import math

        # Convert to radians
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lng = math.radians(lng2 - lng1)

        # Haversine formula
        a = (math.sin(delta_lat / 2) ** 2 +
             math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lng / 2) ** 2)
        c = 2 * math.asin(math.sqrt(a))

        # Earth's radius in kilometers
        r = 6371.0

        return c * r

    def _snap_coordinates(self, lat: float, lng: float) -> Tuple[float, float]:
        """
        Snap coordinates to grid for cache key consistency.

        Rounds to configured precision (default 2 decimal places = ~1.1km grid).

        Args:
            lat: Latitude
            lng: Longitude

        Returns:
            Tuple of (snapped_lat, snapped_lng)
        """
        precision = self.config.coordinate_precision
        lat_snapped = round(lat, precision)
        lng_snapped = round(lng, precision)
        return lat_snapped, lng_snapped

    def _build_cache_key(
        self, center_lat: float, center_lng: float, radius_km: Optional[float] = None
    ) -> str:
        """
        Build cache key for proximal recommendations (user-agnostic).

        Format: proximal:geo:g_{lat_cell}_{lng_cell}:r{radius}

        Args:
            center_lat: Center latitude
            center_lng: Center longitude
            radius_km: Radius in km (uses config default if not provided)

        Returns:
            Cache key string
        """
        lat_snapped, lng_snapped = self._snap_coordinates(center_lat, center_lng)
        radius = radius_km or self.config.large_radius_km

        # User-agnostic key - shared across all users at this location.
        # The `:v7` suffix is the schema version of the cached candidate
        # payload; bump it whenever the on-disk shape changes (v7 = full
        # pillar payload + quality_score compatibility alias +
        # quality_bias). Existing
        # entries with no version (or older versions) are simply skipped
        # by the lookup and the cache rebuilds on the next miss.
        key = f"proximal:geo:g_{lat_snapped}_{lng_snapped}:r{int(radius)}:v7"
        return key

    def _compress_data(self, data: Dict[str, Any]) -> bytes:
        """
        Compress data for storage.

        Args:
            data: Dictionary to compress

        Returns:
            Compressed bytes
        """
        json_str = json.dumps(data)
        if self.config.compression_enabled:
            return gzip.compress(json_str.encode("utf-8"))
        return json_str.encode("utf-8")

    def _decompress_data(self, data: bytes) -> Dict[str, Any]:
        """
        Decompress cached data.

        Args:
            data: Compressed bytes

        Returns:
            Decompressed dictionary
        """
        try:
            if self.config.compression_enabled:
                decompressed = gzip.decompress(data)
                return json.loads(decompressed.decode("utf-8"))
            return json.loads(data.decode("utf-8"))
        except Exception as exc:
            logger.error("Failed to decompress cache data: %s", exc)
            raise

    # ============================================================================
    # Magic Search cache helpers
    # ============================================================================

    MAGIC_INTENT_TTL = 7 * 24 * 60 * 60
    MAGIC_INTENT_KEY_VERSION = "v4"
    MAGIC_GOOGLE_TEXT_TTL = 6 * 60 * 60
    MAGIC_PLACE_DETAILS_TTL = 14 * 24 * 60 * 60
    MAGIC_FINAL_RESULTS_TTL = 10 * 60

    def _magic_cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        if not self.is_available:
            return None
        try:
            raw = self._redis_client.get(key)
            if raw is None:
                return None
            return self._decompress_data(raw)
        except RedisError as exc:
            logger.warning("Redis error reading Magic cache key %s: %s", key, exc)
            return None
        except Exception as exc:
            logger.warning("Unexpected error reading Magic cache key %s: %s", key, exc)
            return None

    def _magic_cache_set(
        self, key: str, payload: Dict[str, Any], ttl_seconds: int
    ) -> bool:
        if not self.is_available:
            return False
        try:
            self._redis_client.setex(key, ttl_seconds, self._compress_data(payload))
            return True
        except RedisError as exc:
            logger.error("Redis error writing Magic cache key %s: %s", key, exc)
            return False
        except Exception as exc:
            logger.error("Unexpected error writing Magic cache key %s: %s", key, exc)
            return False

    def get_magic_intent(self, normalised_prompt: str) -> Optional[Dict[str, Any]]:
        return self._magic_cache_get(
            f"magic:intent:{self.MAGIC_INTENT_KEY_VERSION}:{normalised_prompt}"
        )

    def set_magic_intent(
        self, normalised_prompt: str, payload: Dict[str, Any]
    ) -> bool:
        return self._magic_cache_set(
            f"magic:intent:{self.MAGIC_INTENT_KEY_VERSION}:{normalised_prompt}",
            payload,
            self.MAGIC_INTENT_TTL,
        )

    def build_magic_google_text_key(
        self,
        query: str,
        *,
        lat: float,
        lng: float,
        radius_km: float,
        included_types: Optional[Sequence[str]] = None,
        location_rectangle: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> str:
        lat_cell, lng_cell = self._snap_coordinates(lat, lng)
        query_hash = hashlib.sha1(query.strip().lower().encode("utf-8")).hexdigest()[:16]
        types_payload = json.dumps(sorted(included_types or []), separators=(",", ":"))
        types_hash = hashlib.sha1(types_payload.encode("utf-8")).hexdigest()[:12]
        rectangle_payload = json.dumps(
            location_rectangle or {},
            sort_keys=True,
            separators=(",", ":"),
        )
        rectangle_hash = hashlib.sha1(rectangle_payload.encode("utf-8")).hexdigest()[:12]
        radius = int(round(radius_km))
        return (
            f"magic:google:text:v1:{query_hash}:g_{lat_cell}_{lng_cell}:"
            f"r{radius}:types_{types_hash}:rect_{rectangle_hash}"
        )

    def get_magic_google_results(self, key: str) -> Optional[Dict[str, Any]]:
        return self._magic_cache_get(key)

    def set_magic_google_results(
        self,
        key: str,
        payload: Dict[str, Any],
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        return self._magic_cache_set(
            key,
            payload,
            ttl_seconds or self.MAGIC_GOOGLE_TEXT_TTL,
        )

    def get_magic_place_details(
        self, google_place_id: str
    ) -> Optional[Dict[str, Any]]:
        return self._magic_cache_get(f"magic:google:place:v1:{google_place_id}")

    def set_magic_place_details(
        self,
        google_place_id: str,
        payload: Dict[str, Any],
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        return self._magic_cache_set(
            f"magic:google:place:v1:{google_place_id}",
            payload,
            ttl_seconds or self.MAGIC_PLACE_DETAILS_TTL,
        )

    def get_magic_final_results(self, key: str) -> Optional[Dict[str, Any]]:
        return self._magic_cache_get(key)

    def set_magic_final_results(
        self,
        key: str,
        payload: Dict[str, Any],
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        return self._magic_cache_set(
            key,
            payload,
            ttl_seconds or self.MAGIC_FINAL_RESULTS_TTL,
        )

    def get_cached_recommendations(
        self, center_lat: float, center_lng: float, request_radius_km: float = 2.0
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieve cached recommendations from Redis using distance-based lookup.

        Cache is user-agnostic - shared across all users at a geographic location.
        Searches for any cached entry that fully covers the request area.

        Args:
            center_lat: Center latitude of current request
            center_lng: Center longitude of current request
            request_radius_km: Radius of the request in km (default 2.0km)

        Returns:
            Cached data dictionary or None if not found/error
        """
        if not self.is_available:
            return None

        try:
            # Scan all geographic cache keys (user-agnostic)
            pattern = f"proximal:geo:*"

            for cache_key in self._redis_client.scan_iter(match=pattern, count=100):
                try:
                    # Get cached data
                    cached_bytes = self._redis_client.get(cache_key)
                    if cached_bytes is None:
                        continue

                    # Decompress
                    data = self._decompress_data(cached_bytes)

                    # Calculate distance from request to cached center
                    cached_lat = data.get("center_lat")
                    cached_lng = data.get("center_lng")

                    if cached_lat is None or cached_lng is None:
                        continue

                    distance_to_cache = self._haversine_distance(
                        center_lat, center_lng,
                        cached_lat, cached_lng
                    )

                    # Use the EFFECTIVE coverage radius (distance to the
                    # farthest actually-cached candidate). The configured
                    # search radius (`cached_radius_km`) is only an upper
                    # bound — `LIMIT N nearest` truncates the result set
                    # well before that in dense areas. Falling back to the
                    # configured radius here would let a Notting-Hill cache
                    # serve a Shoreditch request and return zero candidates
                    # after the user-radius filter.
                    configured_radius_km = data.get("cached_radius_km", 15.0)
                    effective_radius_km = data.get("effective_radius_km")
                    coverage_radius_km = (
                        effective_radius_km
                        if effective_radius_km is not None
                        else configured_radius_km
                    )

                    # Cache hit only if request area is fully inside the
                    # effective coverage of the cached payload.
                    max_allowed_distance = coverage_radius_km - request_radius_km

                    if distance_to_cache <= max_allowed_distance:
                        num_candidates = data.get("total_count", 0)
                        logger.info(
                            "✅ CACHE HIT at (%.4f, %.4f), "
                            "using cached data from (%.4f, %.4f) [%.2f km away], "
                            "request_radius=%.1f km, effective_coverage=%.1f km "
                            "(configured=%.1f km), max_allowed_distance=%.1f km",
                            center_lat, center_lng,
                            cached_lat, cached_lng,
                            distance_to_cache,
                            request_radius_km,
                            coverage_radius_km,
                            configured_radius_km,
                            max_allowed_distance,
                        )
                        logger.info("📦 Retrieved from cache: %d candidates (user-agnostic)", num_candidates)

                        # Log cache contents sample for debugging
                        if data.get("candidates"):
                            sample = data["candidates"][0]
                            logger.debug(
                                "Cache contents sample: location_id=%s, lat=%s, lng=%s, "
                                "taste_score=%s, quality_score=%s, has_tags=%s",
                                sample.get("location_id"),
                                sample.get("lat"),
                                sample.get("lng"),
                                sample.get("taste_score"),
                                sample.get("quality_score"),
                                "all_tags" in sample
                            )

                        return data

                except Exception as exc:
                    logger.warning("Error checking cache key %s: %s", cache_key, exc)
                    continue

            # No cache found within distance threshold
            logger.debug(
                "Cache miss at (%.4f, %.4f), no cache that covers request_radius=%.1f km",
                center_lat, center_lng, request_radius_km
            )
            return None

        except RedisError as exc:
            logger.error("Redis error retrieving cache: %s", exc)
            return None
        except Exception as exc:
            logger.error("Unexpected error retrieving cache: %s", exc)
            return None

    def set_cached_recommendations(
        self,
        center_lat: float,
        center_lng: float,
        candidates: List[Dict[str, Any]],
    ) -> bool:
        """
        Store recommendations in Redis cache (user-agnostic).

        Cache entries are shared across all users at a geographic location.

        Args:
            center_lat: Center latitude
            center_lng: Center longitude
            candidates: List of location candidates with quality scores

        Returns:
            True if successfully cached, False otherwise
        """
        if not self.is_available:
            return False

        try:
            # Build cache key (user-agnostic)
            cache_key = self._build_cache_key(center_lat, center_lng)

            # Compute the EFFECTIVE coverage radius: the distance to the
            # farthest cached candidate. The SQL fetch is `LIMIT N nearest`,
            # so in dense areas the actual coverage is much smaller than the
            # configured `large_radius_km`. Using the effective radius for
            # hit checks prevents us serving a Notting-Hill-centred payload
            # to a Shoreditch request just because both are within 15 km.
            effective_radius_km: float = 0.0
            if candidates:
                distances = [
                    float(c["distance_km"])
                    for c in candidates
                    if isinstance(c.get("distance_km"), (int, float))
                ]
                if distances:
                    effective_radius_km = max(distances)

            # Cap at the configured search radius (we never claim more
            # coverage than we asked the DB for).
            effective_radius_km = min(
                effective_radius_km or self.config.large_radius_km,
                self.config.large_radius_km,
            )

            # Prepare cache value (store EXACT coordinates for distance-based lookup)
            cache_value = {
                "center_lat": center_lat,      # Store exact, not snapped
                "center_lng": center_lng,      # Store exact, not snapped
                "cached_radius_km": self.config.large_radius_km,
                "effective_radius_km": effective_radius_km,
                "candidates": candidates,
                "total_count": len(candidates),
                "cached_at": datetime.utcnow().isoformat(),
            }

            # Compress and store
            compressed_data = self._compress_data(cache_value)

            # Set with TTL
            self._redis_client.setex(
                cache_key, self.config.unfiltered_cache_ttl, compressed_data
            )

            logger.info(
                "✅ CACHED: %d locations at (%.4f, %.4f), key=%s, ttl=%ds (user-agnostic)",
                len(candidates),
                center_lat,
                center_lng,
                cache_key,
                self.config.unfiltered_cache_ttl,
            )
            logger.info("📦 Cache entry size: %d candidates stored", len(candidates))

            # Log what's being cached (sample for debugging)
            if candidates:
                sample = candidates[0]
                logger.debug(
                    "Caching sample: location_id=%s, lat=%s, lng=%s, "
                    "taste_score=%s, quality_score=%s, "
                    "has_distance=%s, has_proximity=%s, has_final_score=%s, has_tags=%s",
                    sample.get("location_id"),
                    sample.get("lat"),
                    sample.get("lng"),
                    sample.get("taste_score"),
                    sample.get("quality_score"),
                    "distance_km" in sample,
                    "proximity_score" in sample,
                    "final_score" in sample,
                    "all_tags" in sample
                )
            return True

        except RedisError as exc:
            logger.error("Redis error storing cache: %s", exc)
            return False
        except Exception as exc:
            logger.error("Unexpected error storing cache: %s", exc)
            return False

    def invalidate_user_cache(self, user_id: str) -> int:
        """
        Invalidate all cache entries for a specific user.

        Called when user profile is updated (tag affinities change).

        Args:
            user_id: User identifier

        Returns:
            Number of keys deleted
        """
        if not self.is_available:
            return 0

        try:
            # Pattern to match all user's cache entries
            pattern = f"proximal:*:u_{user_id}:*"

            # Find matching keys
            cursor = 0
            deleted_count = 0

            while True:
                cursor, keys = self._redis_client.scan(
                    cursor, match=pattern, count=100
                )
                if keys:
                    deleted = self._redis_client.delete(*keys)
                    deleted_count += deleted

                if cursor == 0:
                    break

            if deleted_count > 0:
                logger.info("Invalidated %d cache entries for user=%s", deleted_count, user_id)
            return deleted_count

        except RedisError as exc:
            logger.error("Redis error invalidating user cache: %s", exc)
            return 0
        except Exception as exc:
            logger.error("Unexpected error invalidating user cache: %s", exc)
            return 0

    def invalidate_geographic_cache(
        self, center_lat: float, center_lng: float, radius_km: float = 20.0
    ) -> int:
        """
        Invalidate cache entries within a geographic area.

        Called when a new location is added or an existing location is updated.

        Args:
            center_lat: Center latitude of affected area
            center_lng: Center longitude of affected area
            radius_km: Radius to invalidate (default 20km)

        Returns:
            Number of keys deleted
        """
        if not self.is_available:
            return 0

        try:
            # Calculate affected grid cells (cell size comes from
            # CacheConfig.coordinate_precision — see settings.py for the
            # precision → cell size mapping).
            cells_to_check = self._get_affected_cells(center_lat, center_lng, radius_km)

            deleted_count = 0
            for cell_lat, cell_lng in cells_to_check:
                pattern = f"proximal:*:g_{cell_lat}_{cell_lng}:*"

                cursor = 0
                while True:
                    cursor, keys = self._redis_client.scan(
                        cursor, match=pattern, count=100
                    )
                    if keys:
                        deleted = self._redis_client.delete(*keys)
                        deleted_count += deleted

                    if cursor == 0:
                        break

            if deleted_count > 0:
                logger.info(
                    "Invalidated %d cache entries within %.1fkm of (%.2f, %.2f)",
                    deleted_count,
                    radius_km,
                    center_lat,
                    center_lng,
                )
            return deleted_count

        except RedisError as exc:
            logger.error("Redis error invalidating geographic cache: %s", exc)
            return 0
        except Exception as exc:
            logger.error("Unexpected error invalidating geographic cache: %s", exc)
            return 0

    def _get_affected_cells(
        self, center_lat: float, center_lng: float, radius_km: float
    ) -> List[Tuple[float, float]]:
        """
        Get list of grid cells affected by a geographic change.

        Args:
            center_lat: Center latitude
            center_lng: Center longitude
            radius_km: Radius in km

        Returns:
            List of (lat, lng) tuples for affected grid cells
        """
        # Approximate: 1 degree latitude ≈ 111km, 1 degree longitude ≈ 111km * cos(lat)
        precision = self.config.coordinate_precision
        cell_size = 10 ** (-precision)  # e.g. 0.01° for precision=2, 0.1° for precision=1

        # Calculate how many cells in each direction
        lat_degrees = radius_km / 111.0
        lng_degrees = radius_km / (111.0 * math.cos(math.radians(center_lat)))

        lat_cells = int(math.ceil(lat_degrees / cell_size))
        lng_cells = int(math.ceil(lng_degrees / cell_size))

        # Get center cell
        center_lat_snapped, center_lng_snapped = self._snap_coordinates(center_lat, center_lng)

        # Generate all cells within radius
        cells = []
        for lat_offset in range(-lat_cells, lat_cells + 1):
            for lng_offset in range(-lng_cells, lng_cells + 1):
                cell_lat = round(center_lat_snapped + (lat_offset * cell_size), precision)
                cell_lng = round(center_lng_snapped + (lng_offset * cell_size), precision)
                cells.append((cell_lat, cell_lng))

        return cells

    def invalidate_pattern(self, pattern: str) -> int:
        """
        Invalidate cache entries matching a pattern.

        Args:
            pattern: Redis key pattern (e.g., "proximal:*")

        Returns:
            Number of keys deleted
        """
        if not self.is_available:
            return 0

        try:
            cursor = 0
            deleted_count = 0

            while True:
                cursor, keys = self._redis_client.scan(cursor, match=pattern, count=100)
                if keys:
                    deleted = self._redis_client.delete(*keys)
                    deleted_count += deleted

                if cursor == 0:
                    break

            if deleted_count > 0:
                logger.info("Invalidated %d cache entries matching pattern: %s", deleted_count, pattern)
            return deleted_count

        except RedisError as exc:
            logger.error("Redis error invalidating pattern %s: %s", pattern, exc)
            return 0
        except Exception as exc:
            logger.error("Unexpected error invalidating pattern %s: %s", pattern, exc)
            return 0

    def get_cache_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache metrics
        """
        if not self.is_available:
            return {"status": "unavailable"}

        try:
            info = self._redis_client.info("stats")
            keyspace_info = self._redis_client.info("keyspace")

            # Count proximal cache keys
            cursor = 0
            proximal_keys = 0
            while True:
                cursor, keys = self._redis_client.scan(
                    cursor, match="proximal:*", count=1000
                )
                proximal_keys += len(keys)
                if cursor == 0:
                    break

            return {
                "status": "available",
                "total_keys": keyspace_info.get(f"db{self.config.redis_db}", {}).get("keys", 0),
                "proximal_cache_keys": proximal_keys,
                "hits": info.get("keyspace_hits", 0),
                "misses": info.get("keyspace_misses", 0),
                "hit_rate": (
                    info.get("keyspace_hits", 0)
                    / max(1, info.get("keyspace_hits", 0) + info.get("keyspace_misses", 0))
                ),
            }

        except RedisError as exc:
            logger.error("Redis error getting cache stats: %s", exc)
            return {"status": "error", "error": str(exc)}
        except Exception as exc:
            logger.error("Unexpected error getting cache stats: %s", exc)
            return {"status": "error", "error": str(exc)}

    # ============================================================================
    # LPA snapshot — global cache of all `location_popularity_app` rows joined
    # to slim `locations` columns. <1000 rows fits in ~50 KB compressed; one
    # global key replaces per-cell PostGIS queries for the engaged-places set.
    # ============================================================================

    LPA_SNAPSHOT_KEY = "lpa:snapshot:v3"  # v3 = quality_bias + legacy quality_score alias
    LEGACY_LPA_SNAPSHOT_KEYS = ("lpa:snapshot:v2",)
    LPA_SNAPSHOT_TTL = 600  # 10 min — reader falls back to DB if expired

    def get_lpa_snapshot(self) -> Optional[List[Dict[str, Any]]]:
        """Read the global engaged-places snapshot. Returns None on miss."""
        if not self.is_available:
            return None
        try:
            for snapshot_key in (self.LPA_SNAPSHOT_KEY, *self.LEGACY_LPA_SNAPSHOT_KEYS):
                raw = self._redis_client.get(snapshot_key)
                if raw is None:
                    continue
                data = self._decompress_data(raw)
                candidates = data.get("candidates") if isinstance(data, dict) else None
                if not candidates:
                    continue
                logger.info(
                    "📦 LPA snapshot hit: %d engaged places (cached_at=%s, key=%s)",
                    len(candidates),
                    data.get("cached_at"),
                    snapshot_key,
                )
                return candidates
            return None
        except RedisError as exc:
            logger.warning("Redis error reading LPA snapshot: %s", exc)
            return None
        except Exception as exc:
            logger.warning("Unexpected error reading LPA snapshot: %s", exc)
            return None

    def set_lpa_snapshot(self, candidates: List[Dict[str, Any]]) -> bool:
        """Write the global engaged-places snapshot."""
        if not self.is_available:
            return False
        try:
            payload = {
                "candidates": candidates,
                "total_count": len(candidates),
                "cached_at": datetime.utcnow().isoformat(),
            }
            self._redis_client.setex(
                self.LPA_SNAPSHOT_KEY,
                self.LPA_SNAPSHOT_TTL,
                self._compress_data(payload),
            )
            logger.info(
                "✅ LPA snapshot cached: %d engaged places, ttl=%ds",
                len(candidates),
                self.LPA_SNAPSHOT_TTL,
            )
            return True
        except RedisError as exc:
            logger.error("Redis error writing LPA snapshot: %s", exc)
            return False
        except Exception as exc:
            logger.error("Unexpected error writing LPA snapshot: %s", exc)
            return False

    def filter_lpa_snapshot_by_radius(
        self,
        candidates: List[Dict[str, Any]],
        center_lat: float,
        center_lng: float,
        radius_km: float,
    ) -> List[Dict[str, Any]]:
        """
        In-memory haversine filter over the LPA snapshot.

        With <1000 rows, this is a microsecond-level Python loop — no need
        for spatial indexes. Returns a list of dicts with `distance_km`
        annotated, sorted by distance ascending.
        """
        if not candidates:
            return []
        out: List[Dict[str, Any]] = []
        for c in candidates:
            lat = c.get("lat")
            lng = c.get("lng")
            if lat is None or lng is None:
                continue
            try:
                d = self._haversine_distance(center_lat, center_lng, float(lat), float(lng))
            except (TypeError, ValueError):
                continue
            if d <= radius_km:
                row = dict(c)
                row["distance_km"] = d
                out.append(row)
        out.sort(key=lambda r: r["distance_km"])
        return out

    def invalidate_lpa_snapshot(self) -> bool:
        """Drop the LPA snapshot — call after an action insert or LPA write."""
        if not self.is_available:
            return False
        try:
            self._redis_client.delete(self.LPA_SNAPSHOT_KEY)
            return True
        except RedisError:
            return False

    # ============================================================================
    # User profile cache — bundles vibe/dietary vectors, friend graph, and
    # action count into one Redis key. Saves ~4 Supabase round-trips per
    # request on the warm-cache path.
    # ============================================================================

    USER_PROFILE_TTL = 600  # 10 min

    def _user_profile_key(self, user_id: str) -> str:
        return f"user:profile:v1:{user_id}"

    def get_user_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        Returns the bundled profile dict or None on miss.

        Shape: {
            "user_id": str,
            "vibe_vector": List[int] | None,
            "dietary_vector": List[int] | None,
            "action_count": int,
            "friends": [{"friend_id": ..., "influence": ...}, ...],
        }
        """
        if not self.is_available:
            return None
        try:
            raw = self._redis_client.get(self._user_profile_key(user_id))
            if raw is None:
                return None
            return self._decompress_data(raw)
        except RedisError as exc:
            logger.warning("Redis error reading user profile %s: %s", user_id, exc)
            return None
        except Exception as exc:
            logger.warning("Unexpected error reading user profile %s: %s", user_id, exc)
            return None

    def set_user_profile(self, user_id: str, profile: Dict[str, Any]) -> bool:
        """Write the bundled profile to Redis."""
        if not self.is_available:
            return False
        try:
            self._redis_client.setex(
                self._user_profile_key(user_id),
                self.USER_PROFILE_TTL,
                self._compress_data(profile),
            )
            return True
        except RedisError as exc:
            logger.error("Redis error writing user profile %s: %s", user_id, exc)
            return False
        except Exception as exc:
            logger.error("Unexpected error writing user profile %s: %s", user_id, exc)
            return False

    def invalidate_user_profile(self, user_id: str) -> bool:
        """Drop the user profile cache — call after profile/friend edits."""
        if not self.is_available:
            return False
        try:
            self._redis_client.delete(self._user_profile_key(user_id))
            return True
        except RedisError:
            return False

    def get_or_build_user_profile(
        self, user_id: str, supabase: Any
    ) -> Optional[Dict[str, Any]]:
        """
        Return the cached user profile if hot, otherwise build it from
        Supabase and cache. Single entry-point for the request path.

        Bundles four queries that previously ran on every request:
          - users (full row, for existence check + vectors)
          - users (slim, for vectors)
          - user_friends (ids + influence)
          - user_location_actions (count)
        """
        cached = self.get_user_profile(user_id)
        if cached is not None:
            return cached

        user_data = supabase.get_user(user_id)
        if not user_data:
            return None

        try:
            friends = supabase.get_user_friends(user_id) or []
        except Exception:
            friends = []

        try:
            action_count = supabase.get_user_action_count(user_id)
        except Exception:
            action_count = 0

        profile = {
            "user_id": user_id,
            "vibe_vector": user_data.get("vibe_tag_affinity"),
            "dietary_vector": user_data.get("dietary_requirement_tag_affinity"),
            "action_count": action_count,
            "friends": friends,
            "exists": True,
        }
        self.set_user_profile(user_id, profile)
        return profile


# Singleton instance
_cache_service: Optional[ProximalCacheService] = None


def get_cache_service() -> ProximalCacheService:
    """Get or create the singleton cache service instance."""
    global _cache_service
    if _cache_service is None:
        _cache_service = ProximalCacheService()
    return _cache_service
