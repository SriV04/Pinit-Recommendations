from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from pinit.api.schemas import (
    AddLocationRequest,
    AddLocationResponse,
    BatchProximalRequest,
    BatchProximalResponse,
    BubbleLocationRecommendation,
    BubbleRequest,
    BubbleResponse,
    FriendSave,
    HealthResponse,
    IndividualScore,
    LocationCoordinatesResponse,
    LocationRecommendation,
    MagicSearchRequest,
    MagicSearchResponse,
    ProximalRequest,
    ProximalResponse,
    TagMatch,
)
from pinit.config.secrets import GOOGLE_PLACE_API_KEY
from pinit.api.services.proximal_service import (
    add_location_emoji,
    classify_location_photo,
    fetch_google_place_details,
    text_search,
)
from pinit.api.services.cache_service import get_cache_service
from pinit.core.recommendation.proximal_recommendation import (
    ProximalConfig,
    build_batch_proximal_recommendations,
    get_location_coordinates,
)
from pinit.core.recommendation.bubble_recommendation import (
    BubbleConfig,
    build_bubble_recommendations,
)
from pinit.integrations.supabase import get_supabase_service

router = APIRouter()
logger = logging.getLogger(__name__)


VIBE_TAG_ORDER = {
    "cafe": 0, "casual": 1, "cozy": 2, "coffee_shop": 3, "bar": 4,
    "elegant": 5, "fine_dining": 6, "food_truck": 7, "hole_in_the_wall": 8, "late_night": 9,
    "live_music": 10, "michelin_starred": 11, "modern": 12, "fast_food": 13, "quiet": 14,
    "romantic": 15, "sports_bar": 16, "trendy": 17, "takeout_friendly": 18, "pub": 19,
    "grocery_store": 20, "brunch": 21, "outdoor_dining": 22, "wavy": 23, "bossman": 24,
}


def _filter_candidates_by_tags(
    candidates: List[Dict[str, Any]],
    cuisine_filters: Optional[List[str]],
    vibe_filters: Optional[List[str]],
) -> List[Dict[str, Any]]:
    """
    Filter cached candidates by cuisine and vibe tags in-memory.

    Cuisine filter (OR logic): the candidate's ``cuisine`` or
    ``cuisine_secondary`` must match at least one of the filter values.

    Vibe filter (AND logic): for every requested vibe, the candidate's
    ``vibe_vector`` value at the corresponding index (looked up via
    VIBE_TAG_ORDER) must be above 50.

    Args:
        candidates: List of cached candidate dictionaries
        cuisine_filters: Optional list of cuisine names (OR logic)
        vibe_filters: Optional list of vibe tag names (AND logic)

    Returns:
        Filtered list of candidates
    """
    if not cuisine_filters and not vibe_filters:
        return candidates

    # Pre-compute vibe vector indices for the requested vibes
    vibe_indices: List[int] = []
    if vibe_filters:
        for vibe_name in vibe_filters:
            idx = VIBE_TAG_ORDER.get(vibe_name)
            if idx is not None:
                vibe_indices.append(idx)

    # Normalise cuisine filters to lowercase for case-insensitive matching
    cuisine_set = {c.lower() for c in cuisine_filters} if cuisine_filters else None

    filtered = []
    for candidate in candidates:
        # Cuisine filter (OR logic) - cuisine or cuisine_secondary must match
        if cuisine_set:
            primary = (candidate.get("cuisine") or candidate.get("cuisine_primary") or "").lower()
            secondary = (candidate.get("cuisine_secondary") or "").lower()
            if primary not in cuisine_set and secondary not in cuisine_set:
                continue

        # Vibe filter (AND logic) - every requested vibe must score above 50
        if vibe_indices:
            vibe_vector = candidate.get("vibe_vector")
            if not vibe_vector:
                continue
            if not all(
                idx < len(vibe_vector) and (vibe_vector[idx] or 0) > 50
                for idx in vibe_indices
            ):
                continue

        filtered.append(candidate)

    return filtered


def _diversify_by_top_tags(
    scored_candidates: List[Dict[str, Any]],
    user_vibe_vec: List[int],
    vibe_tag_order: Dict[str, int],
    max_results: int,
    top_n_tags: int = 5,
) -> List[Dict[str, Any]]:
    """
    Apply proportional diversity sampling across the user's top vibe tags.

    Instead of returning the top max_results purely by final_score (which clusters
    results around the single highest-affinity tag), this allocates result slots to each
    of the user's top N tags proportionally to their affinity scores.

    Example: if the user's top tags are cafe:85, bossman:70, fine-dining:65, sports-bar:63,
    wavy:63 and max_results=20, slots are allocated as ~6 cafe, ~5 bossman, ~4 fine-dining,
    ~3 sports-bar, ~2 wavy. Within each bucket the candidates with the highest tag-specific
    score are selected. The final list is re-sorted by final_score.

    Args:
        scored_candidates: All candidates, already sorted by final_score descending.
        user_vibe_vec: User's vibe affinity vector (int values 0-100 per tag dimension).
        vibe_tag_order: Mapping of tag text → vector index (from SupabaseService).
        max_results: Total number of results to return.
        top_n_tags: Number of top user tags to distribute slots across.

    Returns:
        Diverse list of up to max_results candidates, sorted by final_score.
    """
    index_to_tag = {idx: tag for tag, idx in vibe_tag_order.items()}

    # Extract user's top N tags (by affinity score, skip zeros)
    user_arr = np.array(user_vibe_vec, dtype=float)
    tag_scores = [
        (index_to_tag[i], float(user_arr[i]))
        for i in range(len(user_arr))
        if i in index_to_tag and float(user_arr[i]) > 0
    ]
    tag_scores.sort(key=lambda x: x[1], reverse=True)
    top_tags = tag_scores[:top_n_tags]

    # Fall back to plain top-N if user has no meaningful preferences
    if not top_tags:
        return scored_candidates[:max_results]

    total_weight = sum(score for _, score in top_tags)

    # Compute proportional slot counts using largest-remainder method
    raw_slots = [(tag, score / total_weight * max_results) for tag, score in top_tags]
    floor_slots = {tag: int(s) for tag, s in raw_slots}
    remainder = max_results - sum(floor_slots.values())
    fractional_order = sorted(
        raw_slots, key=lambda x: x[1] - int(x[1]), reverse=True
    )
    for i in range(remainder):
        floor_slots[fractional_order[i][0]] += 1
    slot_map = floor_slots  # tag -> slot count

    selected_ids: set = set()
    result: List[Dict[str, Any]] = []

    for tag_name, slot_count in slot_map.items():
        if slot_count == 0:
            continue
        tag_idx = vibe_tag_order.get(tag_name)
        if tag_idx is None:
            continue

        # Score each un-selected candidate by how well it matches this specific tag,
        # blended with its overall final_score so quality still matters.
        def _tag_affinity_score(c: Dict[str, Any], idx: int = tag_idx) -> float:
            loc_vec = c.get("vibe_vector")
            if not loc_vec or idx >= len(loc_vec):
                tag_raw = 0.0
            else:
                tag_raw = float(loc_vec[idx]) / 100.0  # normalize to [0, 1]
            return 0.7 * tag_raw + 0.3 * c.get("final_score", 0.0)

        candidates_for_tag = [
            c for c in scored_candidates
            if c.get("location_id") not in selected_ids
        ]
        candidates_for_tag.sort(key=_tag_affinity_score, reverse=True)

        for candidate in candidates_for_tag[:slot_count]:
            selected_ids.add(candidate.get("location_id"))
            result.append(candidate)

    # Fill any remaining slots with the best overall-scored candidates not yet picked
    if len(result) < max_results:
        remaining = [
            c for c in scored_candidates
            if c.get("location_id") not in selected_ids
        ]
        result.extend(remaining[: max_results - len(result)])

    # Re-sort by final_score so the ordering is still sensible
    result.sort(key=lambda x: x.get("final_score", 0.0), reverse=True)
    return result[:max_results]


def _rank_cached_candidates(
    candidates: List[Dict[str, Any]],
    request_lat: float,
    request_lng: float,
    request_radius_km: float,
    quality_weight: float,
    vibe_weight: float,
    dietary_weight: float,
    max_results: int,
    cuisine_filters: Optional[List[str]] = None,
    vibe_filters: Optional[List[str]] = None,
    user_id: Optional[str] = None,
    social_weight: float = 0.20,
    collaborative_weight: float = 0.15,
) -> List[Dict[str, Any]]:
    """
    Apply filtering and ranking to cached candidates using 5-component scoring.

    Computes user-specific vibe, dietary, social, and collaborative scores on
    the fly. Quality scores are pre-computed in the database and cached.

    Args:
        candidates: List of cached candidate dictionaries
        request_lat, request_lng: Request center coordinates
        request_radius_km: Request radius for filtering
        quality_weight, vibe_weight, dietary_weight: Original scoring weights
        max_results: Maximum number of results to return
        cuisine_filters: Optional cuisine tag filters (OR logic)
        vibe_filters: Optional vibe tag filters (AND logic)
        user_id: User ID for computing user-specific scores
        social_weight: Weight for friend-based social score
        collaborative_weight: Weight for collaborative filtering score

    Returns:
        List of scored and ranked candidates (includes weights_used metadata)
    """
    from pinit.core.recommendation.proximal_recommendation import (
        compute_adaptive_weights,
        haversine_distance,
    )
    from pinit.core.recommendation.vector_utils import centered_cosine_similarity, dot_product
    from pinit.core.recommendation.social_scoring import compute_social_scores
    from pinit.core.recommendation.collaborative_scoring import compute_collaborative_scores

    supabase = get_supabase_service()

    # Fetch user vectors if user_id is provided (slim query - only vector columns)
    user_vibe_vec = None
    user_dietary_vec = None
    vibe_tag_order = None
    if user_id:
        user_data = supabase.get_user_vectors(user_id)
        if user_data:
            user_vibe_vec = user_data.get("vibe_tag_affinity")
            user_dietary_vec = user_data.get("dietary_requirement_tag_affinity")
        vibe_tag_order = supabase.vibe_tag_order

    # Step 1: Filter by cuisine and vibe tags if specified
    filtered_candidates = _filter_candidates_by_tags(
        candidates, cuisine_filters, vibe_filters
    )

    # Step 2: Filter by radius first to get candidate location IDs
    radius_filtered = []
    for candidate in filtered_candidates:
        lat = candidate.get("lat")
        lng = candidate.get("lng")
        if lat is None or lng is None:
            continue
        distance_km = haversine_distance(request_lat, request_lng, lat, lng)
        if distance_km <= request_radius_km:
            candidate["distance_km"] = distance_km
            radius_filtered.append(candidate)

    if not radius_filtered:
        return []

    candidate_location_ids = [c["location_id"] for c in radius_filtered]

    # Step 3: Compute social and collaborative scores in batch (user-specific)
    social_scores: Dict[int, float] = {}
    friend_attributions: Dict[int, list] = {}
    collab_scores: Dict[int, float] = {}
    has_friends = False
    action_count = 0

    if user_id:
        # Social scoring
        social_scores, friend_attributions = compute_social_scores(
            user_id, candidate_location_ids, supabase
        )
        has_friends = any(s > 0 for s in social_scores.values())

        # Collaborative scoring
        collab_scores = compute_collaborative_scores(
            user_id, candidate_location_ids, supabase
        )

        # Get action count for adaptive weights
        action_count = supabase.get_user_action_count(user_id)

    # Step 4: Compute adaptive weights
    weights = compute_adaptive_weights(
        has_friends=has_friends,
        action_count=action_count,
        quality_w=quality_weight,
        vibe_w=vibe_weight,
        dietary_w=dietary_weight,
        social_w=social_weight,
        collaborative_w=collaborative_weight,
    )

    w_quality = weights["quality"]
    w_vibe = weights["vibe"]
    w_dietary = weights["dietary"]
    w_social = weights["social"]
    w_collab = weights["collaborative"]

    # Step 5: Score each candidate with all 5 components
    scored_candidates = []
    for candidate in radius_filtered:
        loc_id = candidate["location_id"]

        quality_score = candidate.get("quality_score", 0.0)

        # Vibe and dietary (same as before)
        vibe_score = 0.0
        dietary_score = 0.0
        if user_id:
            loc_vibe_vec = candidate.get("vibe_vector")
            loc_dietary_vec = candidate.get("dietary_requirement_vector")
            if user_vibe_vec and loc_vibe_vec:
                vibe_score = centered_cosine_similarity(user_vibe_vec, loc_vibe_vec)
            if user_dietary_vec and loc_dietary_vec:
                dietary_score = dot_product(user_dietary_vec, loc_dietary_vec)

        social_score = social_scores.get(loc_id, 0.0)
        collaborative_score = collab_scores.get(loc_id, 0.0)

        final_score = (
            w_quality * quality_score +
            w_vibe * vibe_score +
            w_dietary * dietary_score +
            w_social * social_score +
            w_collab * collaborative_score
        )

        scored_candidates.append({
            **candidate,
            "vibe_score": vibe_score,
            "dietary_score": dietary_score,
            "quality_score": quality_score,
            "social_score": social_score,
            "collaborative_score": collaborative_score,
            "final_score": final_score,
            "friend_saves": friend_attributions.get(loc_id),
            "weights_used": weights,
        })

    # Step 6: Sort by final score
    scored_candidates.sort(key=lambda x: x.get("final_score", 0), reverse=True)

    # Step 7: Diversify across user's top tags proportionally, then re-sort
    if user_vibe_vec and vibe_tag_order:
        diverse_candidates = _diversify_by_top_tags(
            scored_candidates, user_vibe_vec, vibe_tag_order, max_results
        )
    else:
        diverse_candidates = scored_candidates[:max_results]

    # Step 8: Assign ranks
    for rank, candidate in enumerate(diverse_candidates, start=1):
        candidate["rank"] = rank

    return diverse_candidates


@router.get("/", response_model=Dict[str, str])
async def root() -> Dict[str, str]:
    """Root endpoint."""
    return {
        "message": "Pinit Proximal Recommendations API",
        "version": "1.0.0",
        "docs": "/docs",
    }


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Health check endpoint."""
    supabase = get_supabase_service()

    # Query database for counts (lightweight count-only queries)
    try:
        locations_count = supabase.count_locations()
        users_count = supabase.count_users()
        tags_count = supabase.count_tags()

        return HealthResponse(
            status="healthy",
            timestamp=datetime.utcnow().isoformat(),
            data_loaded=True,
            total_locations=locations_count,
            total_users=users_count,
            total_tags=tags_count,
        )
    except Exception as exc:
        logger.error(f"Health check failed: {exc}")
        return HealthResponse(
            status="unhealthy",
            timestamp=datetime.utcnow().isoformat(),
            data_loaded=False,
            total_locations=0,
            total_users=0,
            total_tags=0,
        )


@router.post("/recommendations/proximal", response_model=ProximalResponse)
async def get_proximal_recommendations(request: ProximalRequest) -> ProximalResponse:
    """
    Get personalized location recommendations within a radius.

    Returns recommendations sorted by a weighted score combining:
    - User taste preferences
    - Proximity to center point
    - Location quality (ratings)
    """
    supabase = get_supabase_service()

    # Validate user exists
    user_data = supabase.get_user(request.user_id)
    if not user_data:
        raise HTTPException(
            status_code=404,
            detail=f"User '{request.user_id}' not found."
        )

    # Log incoming request parameters
    logger.info(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    logger.info(
        "📍 PROXIMAL REQUEST: user=%s, position=(%.4f, %.4f), radius=%.1f km, max_results=%d",
        request.user_id,
        request.latitude,
        request.longitude,
        request.radius_km,
        request.max_results,
    )
    logger.info(
        "⚖️  Weights: quality=%.2f, vibe=%.2f, dietary=%.2f, social=%.2f, collab=%.2f",
        request.quality_weight,
        request.vibe_weight,
        request.dietary_weight,
        request.social_weight,
        request.collaborative_weight,
    )

    # Extract optional filters
    cuisine_filters = None
    vibe_filters = None

    if request.filters:
        cuisine_filters = request.filters.cuisine
        vibe_filters = request.filters.vibe
        logger.info(
            "🔍 Filters: cuisine=%s, vibe=%s",
            cuisine_filters if cuisine_filters else "None",
            vibe_filters if vibe_filters else "None",
        )
    else:
        logger.info("🔍 Filters: None")

    # Create config
    config = ProximalConfig(
        radius_km=request.radius_km,
        max_results=request.max_results,
        quality_weight=request.quality_weight,
        vibe_weight=request.vibe_weight,
        dietary_weight=request.dietary_weight,
        social_weight=request.social_weight,
        collaborative_weight=request.collaborative_weight,
    )

    # Try cache first (user-agnostic - shared across all users)
    cache_service = get_cache_service()
    cached_data = cache_service.get_cached_recommendations(
        request.latitude, request.longitude, request.radius_km
    )

    if cached_data is not None:
        # Cache hit - use shared filtering logic
        # Cached candidates have quality_score pre-computed, compute user-specific scores
        candidates = cached_data.get("candidates", [])

        logger.info(
            "✅ CACHE HIT: Retrieved %d candidates from cache at (%.4f, %.4f) (user-agnostic)",
            len(candidates),
            cached_data.get("center_lat"),
            cached_data.get("center_lng")
        )

        # Apply shared filtering and ranking logic (compute user-specific scores)
        scored_candidates = _rank_cached_candidates(
            candidates=candidates,
            request_lat=request.latitude,
            request_lng=request.longitude,
            request_radius_km=request.radius_km,
            quality_weight=request.quality_weight,
            vibe_weight=request.vibe_weight,
            dietary_weight=request.dietary_weight,
            max_results=request.max_results,
            cuisine_filters=cuisine_filters,
            vibe_filters=vibe_filters,
            user_id=request.user_id,
            social_weight=request.social_weight,
            collaborative_weight=request.collaborative_weight,
        )

        logger.info(
            "📊 Filtered and ranked: %d results returned from %d cached candidates",
            len(scored_candidates),
            len(candidates)
        )

        # Log sample result
        if scored_candidates:
            sample = scored_candidates[0]
            logger.debug(
                "Top result: location_id=%s, distance=%.2f km, "
                "vibe=%.2f, dietary=%.2f, quality=%.2f, social=%.2f, collab=%.2f, final=%.2f",
                sample.get("location_id"),
                sample.get("distance_km"),
                sample.get("vibe_score"),
                sample.get("dietary_score"),
                sample.get("quality_score"),
                sample.get("social_score"),
                sample.get("collaborative_score"),
                sample.get("final_score")
            )

        # Convert to DataFrame for consistent processing
        recs = pd.DataFrame(scored_candidates) if scored_candidates else pd.DataFrame()

        if not recs.empty:
            recs = recs.replace({pd.NA: None, float('nan'): None, np.nan: None})

    else:
        # Cache miss - query locations with quality scores pre-computed
        cache_radius_km = 15.0  # Large radius for caching
        logger.info(
            "❌ CACHE MISS at (%.4f, %.4f), querying locations within %.1f km with quality scores (PostGIS)",
            request.latitude,
            request.longitude,
            cache_radius_km
        )

        # Use spatial query to get nearby locations with quality scores pre-computed
        supabase = get_supabase_service()
        nearby_locations_data = supabase.get_locations_with_quality_scores(
            request.latitude,
            request.longitude,
            cache_radius_km,
            limit=7000  # Increased cache size for better coverage
        )

        if not nearby_locations_data:
            logger.warning("No locations found within %s km, returning empty results", cache_radius_km)
            return ProximalResponse(
                user_id=request.user_id,
                center_lat=request.latitude,
                center_lon=request.longitude,
                radius_km=request.radius_km,
                total_results=0,
                recommendations=[],
                timestamp=datetime.utcnow().isoformat(),
            )

        # Convert to DataFrame - quality_score, vibe_vector, dietary_requirement_vector already included
        nearby_locations_df = pd.DataFrame(nearby_locations_data)
        logger.info(
            f"📍 Spatial query found {len(nearby_locations_df)} locations within {cache_radius_km}km "
            f"with quality scores and vectors pre-loaded"
        )

        # Cache locations with quality scores (user-agnostic)
        if not nearby_locations_df.empty:
            cache_cols = [
                'location_id', 'name', 'vicinity', 'cuisine_primary',
                'rating', 'user_ratings_total', 'price_level',
                'lat', 'lng', 'distance_km',
                'vibe_vector', 'dietary_requirement_vector',  # Need vectors for user-specific scoring
                'quality_score',  # Pre-computed by database
            ]
            cache_cols_available = [col for col in cache_cols if col in nearby_locations_df.columns]
            cache_data = nearby_locations_df[cache_cols_available]
            candidates_to_cache = cache_data.to_dict("records")

            cache_service.set_cached_recommendations(
                request.latitude,
                request.longitude,
                candidates_to_cache,
            )

            logger.info("✅ Cached %d locations (user-agnostic, quality score pre-computed)", len(candidates_to_cache))

            # Use fresh cache to serve request (compute user-specific scores)
            scored_candidates = _rank_cached_candidates(
                candidates=candidates_to_cache,
                request_lat=request.latitude,
                request_lng=request.longitude,
                request_radius_km=request.radius_km,
                quality_weight=request.quality_weight,
                vibe_weight=request.vibe_weight,
                dietary_weight=request.dietary_weight,
                max_results=request.max_results,
                cuisine_filters=cuisine_filters,
                vibe_filters=vibe_filters,
                user_id=request.user_id,
                social_weight=request.social_weight,
                collaborative_weight=request.collaborative_weight,
            )

            logger.info(
                "📊 Computed user-specific scores and ranked: %d results",
                len(scored_candidates)
            )

            # Convert to DataFrame for consistent processing
            recs = pd.DataFrame(scored_candidates) if scored_candidates else pd.DataFrame()

            if not recs.empty:
                recs = recs.replace({pd.NA: None, float('nan'): None, np.nan: None})
        else:
            recs = pd.DataFrame()

    if recs.empty:
        logger.warning(
            "⚠️ NO RESULTS: No recommendations found for user=%s at (%.4f, %.4f) within %.1f km",
            request.user_id,
            request.latitude,
            request.longitude,
            request.radius_km,
        )
        logger.info(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        return ProximalResponse(
            user_id=request.user_id,
            center_lat=request.latitude,
            center_lon=request.longitude,
            radius_km=request.radius_km,
            total_results=0,
            recommendations=[],
            timestamp=datetime.utcnow().isoformat(),
        )

    # Extract weights_used from the first scored candidate (all share the same weights)
    weights_used = None
    if not recs.empty and "weights_used" in recs.columns:
        first_weights = recs.iloc[0].get("weights_used")
        if isinstance(first_weights, dict):
            weights_used = first_weights

    # Convert to response model
    recommendations = []
    for _, row in recs.iterrows():
        # Build friend attribution list
        friend_saves_raw = row.get("friend_saves")
        friend_saves = None
        if friend_saves_raw and isinstance(friend_saves_raw, list):
            friend_saves = [FriendSave(**fs) for fs in friend_saves_raw]

        recommendations.append(
            LocationRecommendation(
                location_id=int(row["location_id"]),
                name=row["name"],
                vicinity=row.get("vicinity") if pd.notna(row.get("vicinity")) else None,
                cuisine_primary=row.get("cuisine_primary") if pd.notna(row.get("cuisine_primary")) else None,
                rating=float(row["rating"]) if pd.notna(row.get("rating")) else None,
                user_ratings_total=int(row["user_ratings_total"])
                if pd.notna(row.get("user_ratings_total"))
                else None,
                price_level=float(row["price_level"]) if pd.notna(row.get("price_level")) else None,
                distance_km=float(row["distance_km"]),
                vibe_score=float(row["vibe_score"]),
                dietary_score=float(row["dietary_score"]),
                quality_score=float(row["quality_score"]),
                social_score=float(row.get("social_score", 0.0)),
                collaborative_score=float(row.get("collaborative_score", 0.0)),
                final_score=float(row["final_score"]),
                rank=int(row["rank"]),
                friend_saves=friend_saves,
            )
        )

    # Log final response summary
    logger.info(
        "✅ REQUEST COMPLETE: Returning %d recommendations for user=%s at (%.4f, %.4f) within %.1f km",
        len(recommendations),
        request.user_id,
        request.latitude,
        request.longitude,
        request.radius_km,
    )
    if recommendations:
        top_rec = recommendations[0]
        logger.info(
            "🥇 Top result: %s (id=%d, distance=%.2f km, final_score=%.3f, social=%.3f)",
            top_rec.name,
            top_rec.location_id,
            top_rec.distance_km,
            top_rec.final_score,
            top_rec.social_score,
        )
    logger.info(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    return ProximalResponse(
        user_id=request.user_id,
        center_lat=request.latitude,
        center_lon=request.longitude,
        radius_km=request.radius_km,
        total_results=len(recommendations),
        recommendations=recommendations,
        weights_used=weights_used,
        timestamp=datetime.utcnow().isoformat(),
    )


@router.post("/locations/add", response_model=AddLocationResponse)
async def add_location_by_place_id(request: AddLocationRequest) -> AddLocationResponse:
    """
    Add a new location to the database by Google Place ID.

    This endpoint will:
    1. Check if the location already exists in the database
    2. If not, fetch details from Google Places API
    3. Process and tag the location
    4. Store it in the database
    """
    supabase = get_supabase_service()

    # Step 1: Check if location already exists
    existing_location = supabase.get_location_by_google_place_id(request.google_place_id)

    if existing_location:
        return AddLocationResponse(
            success=True,
            message="Location already exists in database",
            location_id=existing_location["location_id"],
            google_place_id=request.google_place_id,
            name=existing_location.get("name"),
            tags_count=None,
            already_existed=True,
        )

    # Step 2: Fetch from Google Places API
    place_details = fetch_google_place_details(request.google_place_id)

    if not place_details:
        raise HTTPException(
            status_code=404,
            detail=f"Could not fetch details for Google Place ID: {request.google_place_id}",
        )

    location_id = place_details["location_id"]
    try:
        # Step 4: Generate emoji if requested
        emoji = None
        if request.generate_emoji:
            emoji = add_location_emoji(place_details)
            if emoji:
                supabase.update_location(location_id, emoji=emoji)
                logger.info("Added emoji '%s' for location %s", emoji, location_id)

        # Step 5: Kick off background tasks (photo classification, menu processing, vibe tagging)
        import asyncio
        from pinit.api.services.vibe_tagging import generate_vibe_tags_for_location

        # Background: classify and upload photo
        if request.classify_photo:
            api_key = GOOGLE_PLACE_API_KEY

            async def _classify_and_upload_photo() -> None:
                try:
                    (
                        photo_ref,
                        photo_score,
                        photo_bytes,
                        photo_content_type,
                    ) = classify_location_photo(request.google_place_id, api_key)

                    bg_supabase = get_supabase_service()

                    if photo_bytes:
                        logger.info(
                            "Attempting to upload photo for location %s (%d bytes, type: %s)",
                            location_id, len(photo_bytes), photo_content_type,
                        )
                        try:
                            bg_supabase.upload_location_photo(
                                location_id, photo_bytes, content_type=photo_content_type,
                            )
                            logger.info("✅ Successfully uploaded photo for location %s", location_id)
                        except Exception as exc:
                            logger.error("❌ Failed to upload photo for location %s: %s", location_id, exc)
                    else:
                        logger.warning("No photo bytes available to upload for location %s", location_id)

                    if photo_ref or photo_score:
                        update_data = {}
                        if photo_ref:
                            update_data["photo_reference"] = photo_ref
                        if photo_score:
                            update_data["photo_reference_score"] = photo_score
                        if update_data:
                            bg_supabase.update_location(location_id, **update_data)

                except Exception as exc:
                    logger.error("Background photo classification failed for location %s: %s", location_id, exc)

            asyncio.create_task(_classify_and_upload_photo())
            logger.info("Kicked off background photo classification for location %s", location_id)

        # Background: menu processing + vibe tagging
        website = place_details.get("website")
        if website:
            from pinit.api.services.menu_processing import process_menu_for_location
            cuisine_value = place_details.get("cuisine_primary", "") or ""

            async def _menu_then_vibe() -> None:
                try:
                    result = await process_menu_for_location(
                        location_id=location_id,
                        google_place_id=request.google_place_id,
                        website=website,
                        restaurant_name=place_details.get("name", ""),
                        cuisine_hint=cuisine_value,
                        detect_cuisine=not cuisine_value,
                    )
                    has_summary = result and result.restaurant_description
                    num_runs = 5 if has_summary else 1
                except Exception as exc:
                    logger.error("Menu processing failed for location %s: %s", location_id, exc)
                    num_runs = 1

                try:
                    await generate_vibe_tags_for_location(location_id, num_runs=num_runs)
                except Exception as exc:
                    logger.error("Vibe tag generation failed for location %s: %s", location_id, exc)

            asyncio.create_task(_menu_then_vibe())
            logger.info("Kicked off background menu processing + vibe tagging for location %s", location_id)
        else:
            asyncio.create_task(generate_vibe_tags_for_location(location_id, num_runs=1))
            logger.info("Kicked off background vibe tagging for location %s (no website)", location_id)

        return AddLocationResponse(
            success=True,
            message="Location successfully added",
            location_id=location_id,
            google_place_id=request.google_place_id,
            name=place_details.get("name"),
            tags_count=0,
            already_existed=False,
            emoji=emoji,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error processing location: {exc}") from exc


@router.post("/locations/magic-search", response_model=MagicSearchResponse)
async def magic_search(request: MagicSearchRequest) -> MagicSearchResponse:
    """
    Search Google Places with a free-text prompt, ingest results, and rank them.

    Flow:
    1. Text Search (New) API → raw place dicts with enterprise-tier fields
    2. Upsert any new places into the database via fetch_google_place_details
    3. Load full location rows (quality_score, vibe_vector, etc.) from DB
    4. Rank with _rank_cached_candidates (user-personalised scoring)
    5. Return MagicSearchResponse
    """
    supabase = get_supabase_service()

    # Validate user exists
    user_data = supabase.get_user(request.user_id)
    if not user_data:
        raise HTTPException(status_code=404, detail=f"User '{request.user_id}' not found")

    _FOOD_TYPES = ["restaurant", "cafe", "bar", "bakery", "meal_takeaway", "night_club"]
    _FOOD_TYPES_SET = {
        "restaurant", "cafe", "bar", "food", "bakery",
        "meal_delivery", "meal_takeaway", "night_club",
    }

    # Step 1: Text search — returns raw Places API v1 place dicts
    try:
        places, api_calls = text_search(
            query=request.prompt,
            place_types=_FOOD_TYPES,
            lat=request.latitude,
            lng=request.longitude,
            radius_m=(request.radius_km or 2.0) * 1000,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    logger.info(
        "Magic search: text_search returned %d places for prompt=%r (%d API calls)",
        len(places), request.prompt, api_calls,
    )

    if not places:
        return MagicSearchResponse(
            user_id=request.user_id,
            center_lat=request.latitude,
            center_lon=request.longitude,
            prompt=request.prompt,
            radius_km=request.radius_km,
            total_candidates=0,
            total_ranked=0,
            recommendations=[],
            timestamp=datetime.utcnow().isoformat(),
        )

    # Step 2: Filter to food-type places and collect their google_place_ids
    food_place_ids: List[str] = []
    place_types_by_id: Dict[str, List[str]] = {}
    for place in places:
        gid = place.get("id")
        if not gid:
            continue
        types_raw = place.get("types", [])
        # If the API returned types, enforce the food filter; if absent, allow through
        if types_raw and not any(t in _FOOD_TYPES_SET for t in types_raw):
            continue
        food_place_ids.append(gid)
        place_types_by_id[gid] = types_raw

    # Batch-fetch all already-known locations in a single DB round-trip
    existing_by_gid = supabase.get_locations_by_google_place_ids(food_place_ids)

    # For places not yet in the DB, fetch details + upsert one by one
    location_ids_to_fetch: List[int] = []
    for gid in food_place_ids:
        if gid in existing_by_gid:
            location_ids_to_fetch.append(existing_by_gid[gid]["location_id"])
        else:
            place_details = fetch_google_place_details(gid)
            if not place_details:
                continue
            fetched_types = place_details.get("types", [])
            if isinstance(fetched_types, str):
                fetched_types = [t.strip() for t in fetched_types.split(",")]
            if fetched_types and not any(t in _FOOD_TYPES_SET for t in fetched_types):
                continue
            lid = place_details.get("location_id")
            if lid:
                location_ids_to_fetch.append(lid)

    # Batch-fetch all full rows (quality_score, vibe_vector, etc.) in one query
    candidates: List[Dict[str, Any]] = supabase.get_locations_by_ids(location_ids_to_fetch)

    # Keep only places that are currently open (or whose status is unknown / not stored)
    before_open_filter = len(candidates)
    candidates = [c for c in candidates if c.get("open_now") is not False]
    logger.info(
        "Magic search: %d food locations collected, %d open (filtered %d closed)",
        before_open_filter, len(candidates), before_open_filter - len(candidates),
    )

    if not candidates:
        return MagicSearchResponse(
            user_id=request.user_id,
            center_lat=request.latitude,
            center_lon=request.longitude,
            prompt=request.prompt,
            radius_km=request.radius_km,
            total_candidates=len(places),
            total_ranked=0,
            recommendations=[],
            timestamp=datetime.utcnow().isoformat(),
        )

    logger.info(
        "Magic search: fetched the following candidates for ranking (location_id: name): %s",
        [(c["location_id"], c["name"]) for c in candidates],
    )
    # Step 4: Rank using the shared cache-style ranking (user-personalised scores)
    scored = _rank_cached_candidates(
        candidates=candidates,
        request_lat=request.latitude,
        request_lng=request.longitude,
        request_radius_km=request.radius_km or 2.0,
        quality_weight=0.30,
        vibe_weight=0.25,
        dietary_weight=0.10,
        max_results=request.max_results or 20,
        user_id=request.user_id,
        social_weight=0.20,
        collaborative_weight=0.15,
    )

    # Step 5: Build response
    recommendations = []
    for candidate in scored:
        friend_saves_raw = candidate.get("friend_saves")
        friend_saves = None
        if friend_saves_raw and isinstance(friend_saves_raw, list):
            friend_saves = [FriendSave(**fs) for fs in friend_saves_raw]

        recommendations.append(
            LocationRecommendation(
                location_id=int(candidate["location_id"]),
                name=candidate["name"],
                vicinity=candidate.get("vicinity"),
                cuisine_primary=candidate.get("cuisine_primary"),
                rating=float(candidate["rating"]) if candidate.get("rating") is not None else None,
                user_ratings_total=int(candidate["user_ratings_total"])
                if candidate.get("user_ratings_total") is not None
                else None,
                price_level=float(candidate["price_level"])
                if candidate.get("price_level") is not None
                else None,
                distance_km=float(candidate["distance_km"]),
                vibe_score=float(candidate["vibe_score"]),
                dietary_score=float(candidate["dietary_score"]),
                quality_score=float(candidate["quality_score"]),
                social_score=float(candidate.get("social_score", 0.0)),
                collaborative_score=float(candidate.get("collaborative_score", 0.0)),
                final_score=float(candidate["final_score"]),
                rank=int(candidate["rank"]),
                friend_saves=friend_saves,
            )
        )

    logger.info(
        "Magic search complete: prompt=%r, %d candidates → %d ranked",
        request.prompt, len(places), len(recommendations),
    )

    return MagicSearchResponse(
        user_id=request.user_id,
        center_lat=request.latitude,
        center_lon=request.longitude,
        prompt=request.prompt,
        radius_km=request.radius_km,
        total_candidates=len(places),
        total_ranked=len(recommendations),
        recommendations=recommendations,
        timestamp=datetime.utcnow().isoformat(),
    )


@router.post("/recommendations/bubble", response_model=BubbleResponse)
async def get_bubble_recommendations(request: BubbleRequest) -> BubbleResponse:
    """
    Get group recommendations for multiple users (bubble).

    COMPLETE FLOW:
    1. Filter by geographic radius
    2. OPTIONAL-FILTER by cuisine (OR logic) and vibe (AND logic) if specified
    3. Compute individual vibe scores (centered cosine similarity)
    4. Compute individual dietary scores (dot product) - soft scoring, not hard filter
    5. Aggregate vibe using context-aware strategy
    6. Aggregate dietary using MAX pooling
    7. Combine with quality
    8. Diversify across group's top vibe tags
    9. Rank and return

    KEY FEATURES:
    - Vibe matching: Centered cosine similarity (same as proximal)
    - Dietary matching: Dot product, MAX pooled across users (soft signal, not hard filter)
    - Activity weighting: Prevents inactive users from dominating group preferences
      - < 20 actions: Scaled weight (new/inactive users have reduced influence)
      - >= 20 actions: Full weight (all active users treated equally)
    - Diversification: Results spread across group's top vibe tags proportionally

    CONTEXT-AWARE AGGREGATION:
    - Group size == 2: Uses minimum score (both must like it)
    - Group size >= 3 and high disagreement (variance > 0.15): Uses fairness blend (40% min + 60% avg)
    - Group size >= 3 and low disagreement (variance <= 0.15): Uses weighted average
    """
    supabase = get_supabase_service()

    # Validate all users exist
    invalid_users = []
    for user_id in request.user_ids:
        user_data = supabase.get_user(user_id)
        if not user_data:
            invalid_users.append(user_id)

    if invalid_users:
        raise HTTPException(
            status_code=404,
            detail=f"Users not found: {invalid_users}"
        )

    # Extract optional filters
    cuisine_filters = None
    vibe_filters = None

    if request.filters:
        cuisine_filters = request.filters.cuisine
        vibe_filters = request.filters.vibe

    # Create config
    config = BubbleConfig(
        radius_km=request.radius_km,
        max_results=request.max_results,
        vibe_weight=request.vibe_weight,
        dietary_weight=request.dietary_weight,
        quality_weight=request.quality_weight,
    )

    # Fetch locations from database
    locations_data = supabase.get_locations(limit=10000)
    locations_df = pd.DataFrame(locations_data)

    # Generate bubble recommendations
    recs = build_bubble_recommendations(
        request.user_ids,
        request.latitude,
        request.longitude,
        locations_df,
        cuisine_filters=cuisine_filters,
        vibe_filters=vibe_filters,
        config=config,
    )

    # Handle empty results
    if recs.empty:
        return BubbleResponse(
            user_ids=request.user_ids,
            group_size=len(request.user_ids),
            center_lat=request.latitude,
            center_lon=request.longitude,
            radius_km=request.radius_km,
            total_results=0,
            recommendations=[],
            optional_filters_applied=request.filters,
            locations_before_filtering=0,
            locations_after_filtering=0,
            timestamp=datetime.utcnow().isoformat(),
        )

    # Extract metadata
    locations_before = int(recs.iloc[0]['locations_before_filtering']) if len(recs) > 0 else 0
    locations_after = int(recs.iloc[0]['locations_after_filtering']) if len(recs) > 0 else 0

    # Convert to response model
    recommendations = []
    for _, row in recs.iterrows():
        # Prepare individual scores if requested
        individual_scores = None
        if request.include_individual_scores and 'individual_scores' in row:
            individual_scores = [
                IndividualScore(**score)
                for score in row['individual_scores']
            ]

        # Prepare vibe breakdown if requested
        # NOTE: get_taste_breakdown currently returns empty list (vector-based system)
        vibe_breakdown = None
        # if request.include_vibe_breakdown:
        #     breakdown = get_taste_breakdown(...)
        #     if breakdown:
        #         vibe_breakdown = [TagMatch(**item) for item in breakdown]

        recommendations.append(
            BubbleLocationRecommendation(
                location_id=int(row["location_id"]),
                name=row["name"],
                vicinity=row.get("vicinity") if pd.notna(row.get("vicinity")) else None,
                cuisine_primary=row.get("cuisine_primary") if pd.notna(row.get("cuisine_primary")) else None,
                rating=float(row["rating"]) if pd.notna(row.get("rating")) else None,
                user_ratings_total=int(row["user_ratings_total"])
                if pd.notna(row.get("user_ratings_total"))
                else None,
                price_level=float(row["price_level"]) if pd.notna(row.get("price_level")) else None,
                distance_km=float(row["distance_km"]),
                group_vibe_score=float(row["group_vibe_score"]),
                group_dietary_score=float(row["group_dietary_score"]),
                quality_score=float(row["quality_score"]),
                final_score=float(row["final_score"]),
                rank=int(row["rank"]),
                individual_scores=individual_scores,
                min_individual_score=float(row.get("min_individual_score", 0)),
                max_individual_score=float(row.get("max_individual_score", 0)),
                score_variance=float(row.get("score_variance", 0)),
                vibe_breakdown=vibe_breakdown,
            )
        )

    return BubbleResponse(
        user_ids=request.user_ids,
        group_size=len(request.user_ids),
        center_lat=request.latitude,
        center_lon=request.longitude,
        radius_km=request.radius_km,
        total_results=len(recommendations),
        recommendations=recommendations,
        optional_filters_applied=request.filters,
        locations_before_filtering=locations_before,
        locations_after_filtering=locations_after,
        timestamp=datetime.utcnow().isoformat(),
    )
