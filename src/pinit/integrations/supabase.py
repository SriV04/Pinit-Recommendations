from typing import Optional, List, Dict, Any
from urllib.parse import urlparse
import logging
import time
from supabase import create_client, Client, ClientOptions
from pinit.config.secrets import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

# Import cache service (with lazy loading to avoid circular imports)
_cache_service = None

def _get_cache_service():
    """Lazy load cache service to avoid circular imports."""
    global _cache_service
    if _cache_service is None:
        try:
            from pinit.api.services.cache_service import get_cache_service
            _cache_service = get_cache_service()
        except Exception as exc:
            logging.getLogger(__name__).warning("Failed to load cache service: %s", exc)
            _cache_service = False  # Mark as failed to avoid retrying
    return _cache_service if _cache_service is not False else None

_CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _extension_for_content_type(content_type: Optional[str]) -> str:
    if not content_type:
        return ""
    normalized = content_type.split(";", 1)[0].strip().lower()
    return _CONTENT_TYPE_EXTENSIONS.get(normalized, "")


class SupabaseService:
    """Basic Supabase service for CRUD operations"""
    
    def __init__(self):
        logger = logging.getLogger(__name__)
        url = (SUPABASE_URL or "").strip()
        # Use service role key for full access
        key = (SUPABASE_SERVICE_ROLE_KEY or "").strip()
        
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in environment or Secret Manager")
        
        # Ensure URL has trailing slash for storage endpoints
        if not url.endswith("/"):
            url = url + "/"
        
        # Basic validation + helpful debug logging without leaking secrets
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            masked = url[:32] + ("…" if len(url) > 32 else "")
            logger.error(f"Invalid SUPABASE_URL format: '{masked}'. Expected like 'https://<ref>.supabase.co'")
            raise ValueError("Invalid SUPABASE_URL format")
        logger.debug(f"Initializing SupabaseService for host: {parsed.netloc}")
        
        options = ClientOptions(
            auto_refresh_token=False,
            persist_session=False,
        )
        self.client: Client = create_client(url, key, options)

        self.vibe_tag_order = {
    "cafe": 0, "casual": 1, "cozy": 2, "coffee_shop": 3, "bar": 4,
    "elegant": 5, "fine_dining": 6, "food_truck": 7, "hole_in_the_wall": 8, "late_night": 9,
    "live_music": 10, "michelin_starred": 11, "modern": 12, "fast_food": 13, "quiet": 14,
    "romantic": 15, "sports_bar": 16, "trendy": 17, "takeout_friendly": 18, "pub": 19, "grocery_store": 20, "brunch": 21, "outdoor_dining": 22, "wavy": 23, "bossman": 24
        }

        self.dietary_requirements_order = {
            "halal": 0, "vegan": 1, "gluten-free": 2, "vegetarian": 3, "dairy-free": 4, "nut-free": 5
        }

    # ==================== TAGS CRUD ====================
    
    def create_tag(self, text: str, prompt_description: Optional[str] = None, 
                   tag_type: Optional[str] = None, colour: Optional[str] = None) -> Dict[str, Any]:
        """Create a new tag"""
        data = {"text": text}
        if prompt_description:
            data["prompt_description"] = prompt_description
        if tag_type:
            data["tag_type"] = tag_type
        if colour:
            data["Colour"] = colour
            
        response = self.client.table("tags").insert(data).execute()
        return response.data[0] if response.data else None
    
    def get_tag(self, tag_id: str) -> Optional[Dict[str, Any]]:
        """Get a tag by ID"""
        response = self.client.table("tags").select("*").eq("tag_id", tag_id).execute()
        return response.data[0] if response.data else None
    
    def get_all_tags(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get all tags"""
        response = self.client.table("tags").select("*").limit(limit).execute()
        return response.data
    
    def update_tag(self, tag_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Update a tag"""
        response = self.client.table("tags").update(kwargs).eq("tag_id", tag_id).execute()
        return response.data[0] if response.data else None
    
    def delete_tag(self, tag_id: str) -> bool:
        """Delete a tag"""
        response = self.client.table("tags").delete().eq("tag_id", tag_id).execute()
        return len(response.data) > 0
    
    # ==================== LOCATIONS CRUD ====================
    
    def create_location(self, name: str, **kwargs) -> Dict[str, Any]:
        """Create a new location"""
        data = {"name": name, **kwargs}
        response = self.client.table("locations").insert(data).execute()

        # Invalidate geographic cache if location has coordinates
        if response.data:
            location = response.data[0]
            lat = location.get("lat") or kwargs.get("lat")
            lng = location.get("lng") or kwargs.get("lng")

            if lat is not None and lng is not None:
                cache = _get_cache_service()
                if cache:
                    cache.invalidate_geographic_cache(lat, lng, radius_km=20.0)

        return response.data[0] if response.data else None
    
    def get_location(self, location_id: int) -> Optional[Dict[str, Any]]:
        """Get a location by ID"""
        response = self.client.table("locations").select("*").eq("location_id", location_id).execute()
        return response.data[0] if response.data else None
    
    def get_location_by_google_place_id(self, google_place_id: str) -> Optional[Dict[str, Any]]:
        """Get a location by Google Place ID"""
        response = self.client.table("locations").select("*").eq("google_place_id", google_place_id).execute()
        return response.data[0] if response.data else None
    
    def get_locations(self, limit: int = 100, offset: int = 0, **filters) -> List[Dict[str, Any]]:
        """Get locations with optional filters"""
        query = self.client.table("locations").select("*").limit(limit).offset(offset)
        for key, value in filters.items():
            query = query.eq(key, value)
        response = query.execute()
        return response.data
    
    def get_locations_with_quality_scores(
        self,
        latitude: float,
        longitude: float,
        radius_km: float,
        limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """
        Get locations within a radius with quality scores pre-computed.

        Uses PostGIS geography column for efficient spatial filtering and
        computes quality scores in the database. Results are user-agnostic
        and can be cached by lat/lng for all users.

        Args:
            latitude: Center point latitude
            longitude: Center point longitude
            radius_km: Radius in kilometers
            limit: Maximum number of results

        Returns:
            List of location dictionaries with quality_score field
        """
        logger = logging.getLogger(__name__)
        # PostGIS ST_DWithin uses meters for geography type
        radius_meters = radius_km * 1000
        params = {
            'center_lat': latitude,
            'center_lng': longitude,
            'radius_meters': radius_meters,
            'result_limit': limit,
        }

        last_exc: Exception | None = None
        for attempt in range(3):
            if attempt > 0:
                time.sleep(0.5 * attempt)  # 0.5s, then 1.0s
                logger.warning(
                    "Retrying get_locations_with_quality (attempt %d/3) after: %s",
                    attempt + 1,
                    last_exc,
                )
            try:
                response = self.client.rpc('get_locations_with_quality', params).execute()
                logger.info(
                    "Spatial query returned %d locations within %.1fkm (attempt %d)",
                    len(response.data),
                    radius_km,
                    attempt + 1,
                )
                return response.data
            except Exception as exc:
                last_exc = exc

        # Never fall back to SELECT * on the full locations table — that
        # pulls megabytes of unrelated data per request. Fail loud and
        # return empty so the caller short-circuits.
        logger.error(
            "Spatial RPC get_locations_with_quality failed after 3 attempts: %s. "
            "Returning empty result (no fallback).",
            last_exc,
        )
        return []

    def fetch_lpa_snapshot_rows(self) -> List[Dict[str, Any]]:
        """
        Bulk-fetch every `location_popularity_app` row joined to the slim
        `locations` columns the ranker needs.

        With LPA <1000 rows this is one egress per snapshot refresh, then
        every request reads from Redis. Returns empty list on error so the
        snapshot job can retry; callers fall back to per-request DB lookups.
        """
        logger = logging.getLogger(__name__)
        try:
            # Single embedded-select query: PostgREST joins lp → locations.
            response = (
                self.client.table("location_popularity_app")
                .select(
                    "location_id,saves_count,dislikes_count,been_to_count,"
                    "share_count,app_engagement_score,google_baseline_score,"
                    "video_insight_score,"
                    "locations(name,vicinity,lat,lng,cuisine,cuisine_primary,"
                    "rating,user_ratings_total,price_level,types,emoji,"
                    "vibe_vector,dietary_requirement_vector,google_place_id)"
                )
                .execute()
            )
            rows = response.data or []
            flat: List[Dict[str, Any]] = []
            for row in rows:
                loc = row.pop("locations", None) or {}
                merged = {**row, **loc}
                # has_app_signal=True for everything in this snapshot by definition
                merged["has_app_signal"] = True
                flat.append(merged)
            logger.info("LPA snapshot fetch: %d rows", len(flat))
            return flat
        except Exception as exc:
            logger.error("Error fetching LPA snapshot rows: %s", exc)
            return []

    def get_fill_locations(
        self,
        latitude: float,
        longitude: float,
        radius_km: float,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """
        Spatial query for `locations` rows that are NOT in `location_popularity_app`.

        Used as the fill_set when the LPA snapshot covers the primary set.
        Calls a thin RPC that does the same `NOT EXISTS` filter as
        `get_locations_with_pillars` but skips the engaged-places branch.
        """
        logger = logging.getLogger(__name__)
        radius_meters = radius_km * 1000
        params = {
            "center_lat": latitude,
            "center_lng": longitude,
            "radius_meters": radius_meters,
            "result_limit": limit,
        }
        try:
            response = self.client.rpc("get_fill_locations", params).execute()
            return response.data or []
        except Exception as exc:
            logger.warning(
                "get_fill_locations RPC unavailable (%s); falling back to "
                "get_locations_with_pillars and discarding the primary set",
                exc,
            )
            return []

    def get_bubble_added_locations(self, bubble_id: str) -> List[Dict[str, Any]]:
        """
        Fetch every location added to a bubble along with the user who added
        it and the location's vibe vector.

        Returns rows of {added_by, location_id, vibe_vector}. Used by the
        bubble recommendation flow to bias each user's effective vibe vector
        toward what they've actually shortlisted in this specific bubble.
        """
        logger = logging.getLogger(__name__)
        try:
            response = (
                self.client.table("bubble_locations")
                .select("added_by, location_id, locations(vibe_vector)")
                .eq("bubble_id", bubble_id)
                .execute()
            )
            rows = response.data or []
            # Flatten the embedded `locations` join to top-level vibe_vector.
            flat = []
            for row in rows:
                loc = row.get("locations") or {}
                vibe = loc.get("vibe_vector") if isinstance(loc, dict) else None
                flat.append({
                    "added_by": row.get("added_by"),
                    "location_id": row.get("location_id"),
                    "vibe_vector": vibe,
                })
            return flat
        except Exception as exc:
            logger.error(f"Error fetching bubble added locations for {bubble_id}: {exc}")
            return []

    def refresh_quality_scores(self) -> Optional[int]:
        """Manually trigger the v4 quality score refresh job.

        Calls refresh_location_quality_scores() in Postgres, which aggregates
        signals from user_location_actions and location_reviews and upserts
        location_popularity_app for every location. Useful for backfills and
        for iterating on the formula without waiting for the nightly cron.
        """
        logger = logging.getLogger(__name__)
        try:
            response = self.client.rpc("refresh_location_quality_scores", {}).execute()
            affected = response.data if isinstance(response.data, int) else None
            logger.info("refresh_location_quality_scores returned: %s", affected)
            return affected
        except Exception as exc:
            logger.error("Failed to refresh quality scores: %s", exc)
            return None

    def get_locations_by_google_place_ids(
        self, google_place_ids: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Batch-fetch locations keyed by google_place_id.

        Returns a dict mapping google_place_id → location row for every ID
        that exists in the database.  Missing IDs are simply absent from the
        returned dict.
        """
        if not google_place_ids:
            return {}
        response = (
            self.client.table("locations")
            .select("*")
            .in_("google_place_id", list(google_place_ids))
            .execute()
        )
        return {row["google_place_id"]: row for row in (response.data or [])}

    def get_locations_by_ids(
        self, location_ids: List[int]
    ) -> List[Dict[str, Any]]:
        """Batch-fetch full location rows by primary key.

        Returns rows in an unspecified order; missing IDs are silently skipped.
        """
        if not location_ids:
            return []
        response = (
            self.client.table("locations")
            .select("*")
            .in_("location_id", list(location_ids))
            .execute()
        )
        return response.data or []

    def get_location_without_emoji(self) -> List[Dict[str, Any]]:
        """Get locations without an emoji assigned"""
        query = self.client.table("locations").select("*").is_("emoji", None)
        response = query.execute()
        return response.data
    
    def update_location(self, location_id: int, **kwargs) -> Optional[Dict[str, Any]]:
        """Update a location"""
        # Get current location to find coordinates for cache invalidation
        current_location = self.get_location(location_id)

        response = self.client.table("locations").update(kwargs).eq("location_id", location_id).execute()

        # Invalidate geographic cache if location has coordinates
        if current_location:
            lat = current_location.get("lat")
            lng = current_location.get("lng")

            if lat is not None and lng is not None:
                cache = _get_cache_service()
                if cache:
                    cache.invalidate_geographic_cache(lat, lng, radius_km=20.0)

        return response.data[0] if response.data else None
    
    def delete_location(self, location_id: int) -> bool:
        """Delete a location"""
        response = self.client.table("locations").delete().eq("location_id", location_id).execute()
        return len(response.data) > 0

    def upload_location_photo(
        self,
        location_id: int,
        image_bytes: bytes,
        content_type: Optional[str] = None,
        index: Optional[int] = None,
    ) -> Any:
        """Upload a location photo to the location_photos bucket.

        Object naming follows the photo-pipeline contract:
          * ``index is None`` → primary photo: ``{location_id}.jpg``
          * ``index >= 1``    → extras:       ``{location_id}_{index}.jpg``

        This function **only** uploads bytes. It does not touch
        ``image_stored`` / ``photos`` / ``extra_photos_stored`` on the row —
        callers must use ``mark_location_image_uploaded`` or
        ``mark_location_extra_photos_stored`` so the cache-hit contract is
        upheld (primary + ``photos`` jsonb written atomically).
        """
        import logging
        logger = logging.getLogger(__name__)

        file_options = {
            "content-type": content_type or "image/jpeg",
            "upsert": "true",
        }
        extension = _extension_for_content_type(content_type)
        if index is None:
            object_name = f"{location_id}{extension}"
        else:
            object_name = f"{location_id}_{int(index)}{extension}"

        logger.info(
            "Uploading to Supabase storage: bucket='location_photos', object='%s', size=%d bytes",
            object_name,
            len(image_bytes),
        )

        try:
            result = self.client.storage.from_("location_photos").upload(
                object_name,
                image_bytes,
                file_options,
            )
            logger.info("Supabase storage upload response: %s", result)
            return result
        except Exception as exc:
            logger.error(
                "Supabase storage upload failed for object '%s': %s",
                object_name,
                exc,
                exc_info=True,
            )
            raise

    def mark_location_image_uploaded(
        self,
        location_id: int,
        photos: Optional[List[Dict[str, Any]]],
        photo_reference: Optional[str],
    ) -> None:
        """Atomically flip image_stored=true and persist photos + photo_reference.

        Consolidated replacement for the old 3-call sequence. Calls the
        ``mark_location_image_uploaded`` SECURITY DEFINER RPC, which COALESCEs
        on NULL so passing ``None`` for photos or photo_reference preserves
        the existing value.

        The photos argument is serialised to a JSON string because the
        supabase-py client otherwise sends ``jsonb`` fields as Python lists
        wrapped in the HTTP body, and the RPC expects a JSON scalar.
        """
        import json as _json
        import logging
        logger = logging.getLogger(__name__)
        try:
            self.client.rpc(
                "mark_location_image_uploaded",
                {
                    "p_location_id": int(location_id),
                    "p_photos": photos if photos is not None else None,
                    "p_photo_reference": photo_reference,
                },
            ).execute()
            logger.info(
                "mark_location_image_uploaded: location=%s, photos_len=%s, ref=%s",
                location_id,
                len(photos) if isinstance(photos, list) else None,
                photo_reference,
            )
        except Exception as exc:
            logger.error(
                "mark_location_image_uploaded RPC failed for %s: %s",
                location_id,
                exc,
            )
            raise

    def mark_location_extra_photos_stored(
        self, location_id: int, count: int
    ) -> None:
        """Bump extra_photos_stored monotonically (GREATEST-safe for parallel uploads)."""
        import logging
        logger = logging.getLogger(__name__)
        try:
            self.client.rpc(
                "mark_location_extra_photos_stored",
                {
                    "p_location_id": int(location_id),
                    "p_count": int(count),
                },
            ).execute()
            logger.info(
                "mark_location_extra_photos_stored: location=%s, count=%s",
                location_id,
                count,
            )
        except Exception as exc:
            logger.error(
                "mark_location_extra_photos_stored RPC failed for %s: %s",
                location_id,
                exc,
            )
            raise

    def mark_location_image_unavailable(self, location_id: int) -> None:
        """Negative-cache the photo pipeline: sets image_unavailable=true so
        the client and any retry loops short-circuit instead of refetching.
        """
        import logging
        logger = logging.getLogger(__name__)
        try:
            self.client.rpc(
                "mark_location_image_unavailable",
                {"p_location_id": int(location_id)},
            ).execute()
            logger.info(
                "mark_location_image_unavailable: location=%s", location_id
            )
        except Exception as exc:
            logger.error(
                "mark_location_image_unavailable RPC failed for %s: %s",
                location_id,
                exc,
            )
            raise
    
    
    # ==================== RECOMMENDATION_CANDIDATES CRUD ====================
    
    def create_recommendation_candidate(self, run_id: str, location_id: int, score: float,
                                       rank: Optional[int] = None, reason: Optional[Dict] = None,
                                       features: Optional[Dict] = None) -> Dict[str, Any]:
        """Create a recommendation candidate"""
        data = {
            "run_id": run_id,
            "location_id": location_id,
            "score": score
        }
        if rank is not None:
            data["rank"] = rank
        if reason:
            data["reason"] = reason
        if features:
            data["features"] = features
            
        response = self.client.table("recommendation_candidates").insert(data).execute()
        return response.data[0] if response.data else None
    
    def get_recommendation_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        """Get a recommendation candidate by ID"""
        response = self.client.table("recommendation_candidates").select("*").eq("candidate_id", candidate_id).execute()
        return response.data[0] if response.data else None
    
    def get_recommendation_candidates(self, run_id: Optional[str] = None,
                                     location_id: Optional[int] = None,
                                     limit: int = 100) -> List[Dict[str, Any]]:
        """Get recommendation candidates, optionally filtered"""
        query = self.client.table("recommendation_candidates").select("*").limit(limit)
        if run_id:
            query = query.eq("run_id", run_id)
        if location_id:
            query = query.eq("location_id", location_id)
        response = query.execute()
        return response.data
    
    def update_recommendation_candidate(self, candidate_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Update a recommendation candidate"""
        response = self.client.table("recommendation_candidates").update(kwargs).eq("candidate_id", candidate_id).execute()
        return response.data[0] if response.data else None
    
    def delete_recommendation_candidate(self, candidate_id: str) -> bool:
        """Delete a recommendation candidate"""
        response = self.client.table("recommendation_candidates").delete().eq("candidate_id", candidate_id).execute()
        return len(response.data) > 0
    
    # ==================== USERS CRUD ====================

    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get a user by supabase_id with their vector affinities."""
        logger = logging.getLogger(__name__)
        try:
            response = self.client.table("users").select("*").eq("supabase_id", user_id).execute()
            return response.data[0] if response.data else None
        except Exception as exc:
            logger.error(f"Error getting user {user_id}: {exc}")
            return None

    def get_user_vectors(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get only the vector affinities needed for recommendation scoring."""
        logger = logging.getLogger(__name__)
        try:
            response = (self.client.table("users")
                       .select("supabase_id, vibe_tag_affinity, dietary_requirement_tag_affinity")
                       .eq("supabase_id", user_id)
                       .execute())
            return response.data[0] if response.data else None
        except Exception as exc:
            logger.error(f"Error getting user vectors for {user_id}: {exc}")
            return None

    def get_users(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get all users with their vector affinities."""
        logger = logging.getLogger(__name__)
        try:
            query = self.client.table("users").select("*")
            if limit:
                query = query.limit(limit)
            response = query.execute()
            return response.data if response.data else []
        except Exception as exc:
            logger.error(f"Error getting users: {exc}")
            return []

    # ==================== USER_TAG_AFFINITIES CRUD ====================

    def create_user_tag_affinity(self, user_id: str, tag_id: str, affinity: float,
                                evidence: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Create or upsert a user tag affinity.

        Args:
            user_id: User UUID
            tag_id: Tag UUID
            affinity: User preference score (0-100 scale)
            evidence: Optional metadata about how affinity was calculated
        """
        data = {
            "user_id": user_id,
            "tag_id": tag_id,
            "affinity": affinity
        }
        if evidence:
            data["evidence"] = evidence

        response = self.client.table("user_tag_affinities").upsert(data).execute()

        # Invalidate user's cache entries
        cache = _get_cache_service()
        if cache:
            cache.invalidate_user_cache(user_id)

        return response.data[0] if response.data else None
    
    def get_user_tag_affinity(self, user_id: str, tag_id: str) -> Optional[Dict[str, Any]]:
        """Get a user tag affinity"""
        response = (self.client.table("user_tag_affinities")
                   .select("*")
                   .eq("user_id", user_id)
                   .eq("tag_id", tag_id)
                   .execute())
        return response.data[0] if response.data else None
    
    def get_user_tag_affinities(self, user_id: Optional[str] = None,
                               tag_id: Optional[str] = None,
                               min_affinity: Optional[float] = None) -> List[Dict[str, Any]]:
        """
        Get user tag affinities with optional filters.
        
        Args:
            user_id: Optional user UUID to filter by
            tag_id: Optional tag UUID to filter by
            min_affinity: Optional minimum affinity threshold (0-100 scale)
        
        Returns:
            List of affinity records with affinity values on 0-100 scale
        """
        query = self.client.table("user_tag_affinities").select("*")
        if user_id:
            query = query.eq("user_id", user_id)
        if tag_id:
            query = query.eq("tag_id", tag_id)
        if min_affinity is not None:
            query = query.gte("affinity", min_affinity)
        response = query.execute()
        return response.data
    
    def update_user_tag_affinity(self, user_id: str, tag_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Update a user tag affinity"""
        response = (self.client.table("user_tag_affinities")
                   .update(kwargs)
                   .eq("user_id", user_id)
                   .eq("tag_id", tag_id)
                   .execute())

        # Invalidate user's cache entries
        cache = _get_cache_service()
        if cache:
            cache.invalidate_user_cache(user_id)

        return response.data[0] if response.data else None
    
    def delete_user_tag_affinity(self, user_id: str, tag_id: str) -> bool:
        """Delete a user tag affinity"""
        response = (self.client.table("user_tag_affinities")
                   .delete()
                   .eq("user_id", user_id)
                   .eq("tag_id", tag_id)
                   .execute())
        return len(response.data) > 0

    # ==================== COUNT HELPERS ====================

    def count_locations(self) -> int:
        """Get total count of locations without fetching rows."""
        response = self.client.table("locations").select("location_id", count="exact").limit(0).execute()
        return response.count if hasattr(response, 'count') and response.count is not None else 0

    def count_users(self) -> int:
        """Get total count of users without fetching rows."""
        response = self.client.table("users").select("supabase_id", count="exact").limit(0).execute()
        return response.count if hasattr(response, 'count') and response.count is not None else 0

    def count_tags(self) -> int:
        """Get total count of tags without fetching rows."""
        response = self.client.table("tags").select("tag_id", count="exact").limit(0).execute()
        return response.count if hasattr(response, 'count') and response.count is not None else 0

    # ==================== USER_LOCATION_ACTIONS CRUD ====================

    def get_user_location_actions(self, user_id: Optional[str] = None,
                                  location_id: Optional[int] = None,
                                  action_type: Optional[str] = None,
                                  limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get user location actions (check-ins, saves, ratings, etc.).

        Args:
            user_id: Optional user ID to filter by
            location_id: Optional location ID to filter by
            action_type: Optional action type (e.g., 'been_to', 'save', 'like',
                'dislike', 'bubble_save', 'shared_video')
            limit: Optional limit on results

        Returns:
            List of action records
        """
        query = self.client.table("user_location_actions").select("*")
        if user_id:
            query = query.eq("user_id", user_id)
        if location_id:
            query = query.eq("location_id", location_id)
        if action_type:
            query = query.eq("action_type", action_type)
        if limit:
            query = query.limit(limit)
        response = query.execute()
        return response.data

    # ==================== SOCIAL / FRIEND QUERIES ====================

    def get_user_friends(self, user_id: str) -> List[Dict[str, Any]]:
        """
        Get accepted friends for a user (both directions of the relationship).

        Returns a deduplicated list of friend records with friend_id and influence.
        """
        logger = logging.getLogger(__name__)
        try:
            # User is the follower (they follow others)
            resp1 = (
                self.client.table("user_friends")
                .select("followee_id, influence")
                .eq("follower_id", user_id)
                .eq("status", "accepted")
                .execute()
            )
            # User is the followee (others follow them)
            resp2 = (
                self.client.table("user_friends")
                .select("follower_id, influence")
                .eq("followee_id", user_id)
                .eq("status", "accepted")
                .execute()
            )

            friends = {}
            for row in (resp1.data or []):
                fid = row["followee_id"]
                friends[fid] = {"friend_id": fid, "influence": row.get("influence")}
            for row in (resp2.data or []):
                fid = row["follower_id"]
                if fid not in friends:
                    friends[fid] = {"friend_id": fid, "influence": row.get("influence")}

            return list(friends.values())
        except Exception as exc:
            logger.error(f"Error fetching friends for {user_id}: {exc}")
            return []

    def get_friends_location_actions(
        self, friend_ids: List[str], location_ids: List[int]
    ) -> List[Dict[str, Any]]:
        """
        Batch-fetch friends' actions on candidate locations.

        Returns list of {user_id, location_id, action, created_at} dicts.
        """
        logger = logging.getLogger(__name__)
        if not friend_ids or not location_ids:
            return []
        try:
            response = (
                self.client.table("user_location_actions")
                .select("user_id, location_id, action, created_at")
                .in_("user_id", list(friend_ids))
                .in_("location_id", list(location_ids))
                .execute()
            )
            return response.data or []
        except Exception as exc:
            logger.error(f"Error fetching friend actions: {exc}")
            return []

    def get_friend_profiles(self, friend_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Slim query to get friend display info for attribution.

        Returns {friend_id: {name, username, profile_image_url}}.
        """
        logger = logging.getLogger(__name__)
        if not friend_ids:
            return {}
        try:
            response = (
                self.client.table("users")
                .select("supabase_id, name, username, profile_image_url")
                .in_("supabase_id", list(friend_ids))
                .execute()
            )
            return {
                row["supabase_id"]: row
                for row in (response.data or [])
            }
        except Exception as exc:
            logger.error(f"Error fetching friend profiles: {exc}")
            return {}

    # ==================== COLLABORATIVE FILTERING QUERIES ====================

    def get_user_positive_actions(self, user_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Fetch user's recent positive actions.

        Action enum values that count as positive: save, been_to, like,
        bubble_save, shared_video.

        Returns list of {location_id, action, created_at} ordered by recency.
        """
        logger = logging.getLogger(__name__)
        try:
            response = (
                self.client.table("user_location_actions")
                .select("location_id, action, created_at")
                .eq("user_id", user_id)
                .in_("action", ["save", "been_to", "like", "bubble_save", "shared_video"])
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return response.data or []
        except Exception as exc:
            logger.error(f"Error fetching positive actions for {user_id}: {exc}")
            return []

    def get_location_similarities(
        self, location_ids: List[int], saved_location_ids: List[int]
    ) -> List[Dict[str, Any]]:
        """
        Fetch precomputed similarities between candidate locations and user's saved set.

        Returns list of {location_id_a, location_id_b, similarity_score}.
        location_id_a = user's saved location, location_id_b = candidate.
        """
        logger = logging.getLogger(__name__)
        if not location_ids or not saved_location_ids:
            return []
        try:
            response = (
                self.client.table("location_similarities")
                .select("location_id_a, location_id_b, similarity_score")
                .in_("location_id_a", list(saved_location_ids))
                .in_("location_id_b", list(location_ids))
                .execute()
            )
            return response.data or []
        except Exception as exc:
            logger.error(f"Error fetching location similarities: {exc}")
            return []

    def get_all_positive_actions(self, limit: int = 50000) -> List[Dict[str, Any]]:
        """
        Fetch all positive actions across all users for building similarity matrix.

        Used by the batch similarity_builder job.
        """
        logger = logging.getLogger(__name__)
        try:
            response = (
                self.client.table("user_location_actions")
                .select("user_id, location_id, action, created_at")
                .in_("action", ["save", "been_to", "like", "bubble_save", "shared_video"])
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return response.data or []
        except Exception as exc:
            logger.error(f"Error fetching all positive actions: {exc}")
            return []

    def upsert_location_similarities(self, rows: List[Dict[str, Any]]) -> int:
        """
        Batch upsert location similarity records.

        Args:
            rows: List of {location_id_a, location_id_b, similarity_score, co_save_count}

        Returns:
            Number of rows upserted
        """
        logger = logging.getLogger(__name__)
        if not rows:
            return 0
        try:
            response = (
                self.client.table("location_similarities")
                .upsert(rows)
                .execute()
            )
            return len(response.data) if response.data else 0
        except Exception as exc:
            logger.error(f"Error upserting location similarities: {exc}")
            return 0

    # ==================== ACTION COUNTS ====================

    def get_user_action_count(self, user_id: str) -> int:
        """
        Get total number of actions for a user.

        Args:
            user_id: User ID

        Returns:
            Total count of actions
        """
        response = (self.client.table("user_location_actions")
                   .select("user_id", count="exact")
                   .eq("user_id", user_id)
                   .limit(0)
                   .execute())
        return response.count if hasattr(response, 'count') and response.count is not None else 0

    # ==================== VIDEO INSIGHTS ====================

    def get_video_insights_for_location(
        self, location_id: int
    ) -> List[Dict[str, Any]]:
        """Fetch all video_insights rows for a location.

        Returns list of dicts with vibe_signals, sentiment, key_dishes,
        creator_notes for every video associated with this location.
        Used by the vibe tagging pipeline to blend TikTok signals into
        the LLM-scored vibe vector.
        """
        logger = logging.getLogger(__name__)
        try:
            response = (
                self.client.table("video_insights")
                .select("vibe_signals, sentiment, key_dishes, creator_notes")
                .eq("location_id", location_id)
                .execute()
            )
            return response.data or []
        except Exception as exc:
            logger.error(
                "Error fetching video_insights for location %s: %s",
                location_id, exc,
            )
            return []


# Singleton instance
_supabase_service = None


def get_supabase_service() -> SupabaseService:
    """Get or create the singleton SupabaseService instance"""
    global _supabase_service
    if _supabase_service is None:
        _supabase_service = SupabaseService()
    return _supabase_service
