from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Dict

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from pinit.api.schemas import (
    AddLocationRequest,
    AddLocationResponse,
    BatchProximalRequest,
    BatchProximalResponse,
    HealthResponse,
    LocationCoordinatesResponse,
    LocationRecommendation,
    MagicSearchRequest,
    MagicSearchResponse,
    ProximalRequest,
    ProximalResponse,
    TagMatch,
)
from pinit.config.secrets import GOOGLE_MAPS_API_KEY
from pinit.api.services.proximal_service import (
    DATA_CACHE,
    classify_location_photo,
    fetch_google_place_details,
    get_taste_breakdown,
    process_and_tag_location,
    search_google_places,
)
from pinit.core.recommendation.proximal_recommendation import (
    ProximalConfig,
    build_batch_proximal_recommendations,
    build_proximal_recommendations,
    get_location_coordinates,
)
from pinit.integrations.supabase import get_supabase_service

router = APIRouter()
logger = logging.getLogger(__name__)


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
    return HealthResponse(
        status="healthy" if DATA_CACHE["loaded"] else "loading",
        timestamp=datetime.utcnow().isoformat(),
        data_loaded=DATA_CACHE["loaded"],
        total_locations=len(DATA_CACHE["locations"]) if DATA_CACHE["loaded"] else 0,
        total_users=DATA_CACHE["user_tags"]["user_id"].nunique() if DATA_CACHE["loaded"] else 0,
        total_tags=len(DATA_CACHE["tags"]) if DATA_CACHE["loaded"] else 0,
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
    if not DATA_CACHE["loaded"]:
        raise HTTPException(status_code=503, detail="Data still loading, please try again")

    # Validate user exists
    if request.user_id not in DATA_CACHE["user_tags"]["user_id"].values:
        raise HTTPException(
            status_code=404,
            detail=(
                f"User '{request.user_id}' not found. "
                f"Available users: {DATA_CACHE['user_tags']['user_id'].unique().tolist()[:5]}"
            ),
        )

    # Create config
    config = ProximalConfig(
        radius_km=request.radius_km,
        max_results=request.max_results,
        taste_weight=request.taste_weight,
        proximity_weight=request.proximity_weight,
        quality_weight=request.quality_weight,
    )

    # Generate recommendations
    recs = build_proximal_recommendations(
        request.user_id,
        request.latitude,
        request.longitude,
        DATA_CACHE["locations"],
        DATA_CACHE["user_tags"],
        DATA_CACHE["location_tags"],
        config,
    )

    if recs.empty:
        return ProximalResponse(
            user_id=request.user_id,
            center_lat=request.latitude,
            center_lon=request.longitude,
            radius_km=request.radius_km,
            total_results=0,
            recommendations=[],
            timestamp=datetime.utcnow().isoformat(),
        )

    # Convert to response model
    recommendations = []
    for _, row in recs.iterrows():
        # Get taste breakdown if requested
        taste_breakdown = None
        if request.include_taste_breakdown:
            breakdown = get_taste_breakdown(
                location_id=int(row["location_id"]),
                user_id=request.user_id,
                user_tags=DATA_CACHE["user_tags"],
                location_tags=DATA_CACHE["location_tags"],
                top_n=5,
            )
            if breakdown:
                taste_breakdown = [TagMatch(**item) for item in breakdown]

        recommendations.append(
            LocationRecommendation(
                location_id=int(row["location_id"]),
                name=row["name"],
                vicinity=row.get("vicinity"),
                cuisine_primary=row.get("cuisine_primary"),
                rating=float(row["rating"]) if pd.notna(row.get("rating")) else None,
                user_ratings_total=int(row["user_ratings_total"])
                if pd.notna(row.get("user_ratings_total"))
                else None,
                price_level=float(row["price_level"]) if pd.notna(row.get("price_level")) else None,
                distance_km=float(row["distance_km"]),
                taste_score=float(row["taste_score"]),
                proximity_score=float(row["proximity_score"]),
                quality_score=float(row["quality_score"]),
                final_score=float(row["final_score"]),
                rank=int(row["rank"]),
                taste_breakdown=taste_breakdown,
            )
        )

    return ProximalResponse(
        user_id=request.user_id,
        center_lat=request.latitude,
        center_lon=request.longitude,
        radius_km=request.radius_km,
        total_results=len(recommendations),
        recommendations=recommendations,
        timestamp=datetime.utcnow().isoformat(),
    )


@router.post("/recommendations/proximal/batch", response_model=BatchProximalResponse)
async def get_batch_proximal_recommendations(
    request: BatchProximalRequest,
) -> BatchProximalResponse:
    """Get proximal recommendations for multiple users at once."""
    if not DATA_CACHE["loaded"]:
        raise HTTPException(status_code=503, detail="Data still loading, please try again")

    # Validate users exist
    available_users = set(DATA_CACHE["user_tags"]["user_id"].unique())
    invalid_users = [uid for uid in request.user_ids if uid not in available_users]

    if invalid_users:
        raise HTTPException(status_code=404, detail=f"Users not found: {invalid_users[:5]}")

    # Create config
    config = ProximalConfig(
        radius_km=request.radius_km,
        max_results=request.max_results,
    )

    # Generate batch recommendations
    batch_recs = build_batch_proximal_recommendations(
        request.user_ids,
        request.latitude,
        request.longitude,
        DATA_CACHE["locations"],
        DATA_CACHE["user_tags"],
        DATA_CACHE["location_tags"],
        config,
    )

    # Group by user
    results = []
    for user_id in request.user_ids:
        user_recs = batch_recs[batch_recs["user_id"] == user_id]

        recommendations = []
        for _, row in user_recs.iterrows():
            recommendations.append(
                LocationRecommendation(
                    location_id=int(row["location_id"]),
                    name=row["name"],
                    vicinity=row.get("vicinity"),
                    cuisine_primary=row.get("cuisine_primary"),
                    rating=float(row["rating"]) if pd.notna(row.get("rating")) else None,
                    user_ratings_total=int(row["user_ratings_total"])
                    if pd.notna(row.get("user_ratings_total"))
                    else None,
                    price_level=float(row["price_level"]) if pd.notna(row.get("price_level")) else None,
                    distance_km=float(row["distance_km"]),
                    taste_score=float(row["taste_score"]),
                    proximity_score=float(row["proximity_score"]),
                    quality_score=float(row["quality_score"]),
                    final_score=float(row["final_score"]),
                    rank=int(row["rank"]),
                )
            )

        results.append(
            ProximalResponse(
                user_id=user_id,
                center_lat=request.latitude,
                center_lon=request.longitude,
                radius_km=request.radius_km,
                total_results=len(recommendations),
                recommendations=recommendations,
                timestamp=datetime.utcnow().isoformat(),
            )
        )

    return BatchProximalResponse(
        center_lat=request.latitude,
        center_lon=request.longitude,
        radius_km=request.radius_km,
        total_users=len(request.user_ids),
        total_recommendations=len(batch_recs) if not batch_recs.empty else 0,
        results=results,
        timestamp=datetime.utcnow().isoformat(),
    )


@router.get("/locations/{location_id}/coordinates", response_model=LocationCoordinatesResponse)
async def get_coordinates(location_id: int) -> LocationCoordinatesResponse:
    """Get coordinates for a specific location."""
    if not DATA_CACHE["loaded"]:
        raise HTTPException(status_code=503, detail="Data still loading, please try again")

    coords = get_location_coordinates(location_id, DATA_CACHE["locations"])

    if coords is None:
        return LocationCoordinatesResponse(
            location_id=location_id,
            latitude=None,
            longitude=None,
            found=False,
        )

    lat, lon = coords
    return LocationCoordinatesResponse(
        location_id=location_id,
        latitude=lat,
        longitude=lon,
        found=True,
    )


@router.get("/users", response_model=Dict[str, Any])
async def list_users(limit: int = Query(10, ge=1, le=100)) -> Dict[str, Any]:
    """List available users in the system."""
    if not DATA_CACHE["loaded"]:
        raise HTTPException(status_code=503, detail="Data still loading, please try again")

    users = DATA_CACHE["user_tags"]["user_id"].unique().tolist()[:limit]

    return {
        "total_users": DATA_CACHE["user_tags"]["user_id"].nunique(),
        "users": users,
        "limit": limit,
    }


@router.get("/users/{user_id}/profile", response_model=Dict[str, Any])
async def get_user_profile(user_id: str, top_n: int = Query(10, ge=1, le=50)) -> Dict[str, Any]:
    """Get a user's taste profile (top tag preferences)."""
    if not DATA_CACHE["loaded"]:
        raise HTTPException(status_code=503, detail="Data still loading, please try again")

    user_profile = DATA_CACHE["user_tags"][DATA_CACHE["user_tags"]["user_id"] == user_id]

    if user_profile.empty:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")

    user_profile = user_profile.nlargest(top_n, "score")

    tag_names = DATA_CACHE["tags"].set_index("tag_id")["text"].to_dict()

    preferences = []
    for _, row in user_profile.iterrows():
        tag_name = tag_names.get(row["tag_id"], row.get("tag_text", "Unknown"))
        preferences.append({"tag": tag_name, "score": float(row["score"])})

    return {
        "user_id": user_id,
        "top_preferences": preferences,
        "total_tags": len(user_profile),
    }


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

    # Step 3: Process and tag the location
    try:
        location_id, tags_count = process_and_tag_location(place_details, supabase)

        # Step 4: Classify photo if requested
        photo_ref = None
        photo_score = None

        if request.classify_photo:
            api_key = GOOGLE_MAPS_API_KEY
            if not api_key:
                raise HTTPException(status_code=500, detail="GOOGLE_MAPS_API_KEY not configured")

            (
                photo_ref,
                photo_score,
                photo_bytes,
                photo_content_type,
            ) = classify_location_photo(request.google_place_id, api_key)

            if photo_bytes:
                logger.info(
                    "Attempting to upload photo for location %s (%d bytes, type: %s)",
                    location_id,
                    len(photo_bytes),
                    photo_content_type,
                )
                try:
                    upload_result = supabase.upload_location_photo(
                        location_id,
                        photo_bytes,
                        content_type=photo_content_type,
                    )
                    logger.info(
                        "✅ Successfully uploaded photo for location %s. Result: %s",
                        location_id,
                        upload_result,
                    )
                except Exception as exc:
                    logger.error(
                        "❌ Failed to upload photo for location %s: %s",
                        location_id,
                        exc,
                        exc_info=True,
                    )
            else:
                logger.warning("No photo bytes available to upload for location %s", location_id)

            # Update location with photo data
            if photo_ref or photo_score:
                update_data = {}
                if photo_ref:
                    update_data["photo_reference"] = photo_ref
                if photo_score:
                    update_data["photo_reference_score"] = photo_score

                if update_data:
                    supabase.update_location(location_id, **update_data)

        return AddLocationResponse(
            success=True,
            message="Location successfully added and tagged",
            location_id=location_id,
            google_place_id=request.google_place_id,
            name=place_details.get("name"),
            tags_count=tags_count,
            already_existed=False,
            photo_reference=photo_ref,
            photo_score=photo_score,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error processing location: {exc}") from exc


@router.post("/locations/magic-search", response_model=MagicSearchResponse)
async def magic_search(request: MagicSearchRequest) -> MagicSearchResponse:
    """
    Search Google Places with a free-text prompt, ingest results, and rank them.
    """
    if not DATA_CACHE["loaded"]:
        raise HTTPException(status_code=503, detail="Data still loading, please try again")

    if request.user_id not in DATA_CACHE["user_tags"]["user_id"].values:
        raise HTTPException(status_code=404, detail=f"User '{request.user_id}' not found")

    try:
        place_ids = search_google_places(
            request.prompt,
            request.latitude,
            request.longitude,
            request.radius_km,
            request.max_results,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not place_ids:
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

    supabase = get_supabase_service()
    collected_locations = []
    collected_location_tags = []

    food_types = {"restaurant", "cafe", "bar", "food", "meal_delivery", "meal_takeaway"}

    for place_id in place_ids:
        existing = supabase.get_location_by_google_place_id(place_id)
        if existing:
            location_id = existing["location_id"]
        else:
            place_details = fetch_google_place_details(place_id)
            if not place_details:
                continue
            types = place_details.get("types", [])
            if not any(t in food_types for t in types):
                continue
            try:
                location_id, _ = process_and_tag_location(place_details, supabase)
            except Exception:
                continue

        location = supabase.get_location(location_id)
        if not location:
            continue

        collected_locations.append(location)
        location_tags = supabase.get_location_tags(location_id=location_id)
        if location_tags:
            collected_location_tags.extend(location_tags)

    if not collected_locations:
        return MagicSearchResponse(
            user_id=request.user_id,
            center_lat=request.latitude,
            center_lon=request.longitude,
            prompt=request.prompt,
            radius_km=request.radius_km,
            total_candidates=len(place_ids),
            total_ranked=0,
            recommendations=[],
            timestamp=datetime.utcnow().isoformat(),
        )

    locations_df = pd.DataFrame(collected_locations)
    if collected_location_tags:
        location_tags_df = pd.DataFrame(collected_location_tags)
        if "tag_text" not in location_tags_df.columns:
            tag_id_to_text = {}
            if DATA_CACHE["tags"] is not None:
                tag_id_to_text = DATA_CACHE["tags"].set_index("tag_id")["text"].to_dict()
            location_tags_df["tag_text"] = location_tags_df["tag_id"].map(tag_id_to_text)
    else:
        location_tags_df = pd.DataFrame(columns=["location_id", "tag_id", "score", "tag_text"])

    config = ProximalConfig(
        radius_km=request.radius_km,
        max_results=request.max_results,
    )

    recs = build_proximal_recommendations(
        request.user_id,
        request.latitude,
        request.longitude,
        locations_df,
        DATA_CACHE["user_tags"],
        location_tags_df,
        config,
    )

    recommendations = []
    for _, row in recs.iterrows():
        taste_breakdown = None
        if request.include_taste_breakdown:
            breakdown = get_taste_breakdown(
                location_id=int(row["location_id"]),
                user_id=request.user_id,
                user_tags=DATA_CACHE["user_tags"],
                location_tags=location_tags_df,
                top_n=5,
            )
            if breakdown:
                taste_breakdown = [TagMatch(**item) for item in breakdown]

        recommendations.append(
            LocationRecommendation(
                location_id=int(row["location_id"]),
                name=row["name"],
                vicinity=row.get("vicinity"),
                cuisine_primary=row.get("cuisine_primary"),
                rating=float(row["rating"]) if pd.notna(row.get("rating")) else None,
                user_ratings_total=int(row["user_ratings_total"])
                if pd.notna(row.get("user_ratings_total"))
                else None,
                price_level=float(row["price_level"]) if pd.notna(row.get("price_level")) else None,
                distance_km=float(row["distance_km"]),
                taste_score=float(row["taste_score"]),
                proximity_score=float(row["proximity_score"]),
                quality_score=float(row["quality_score"]),
                final_score=float(row["final_score"]),
                rank=int(row["rank"]),
                taste_breakdown=taste_breakdown,
            )
        )

    return MagicSearchResponse(
        user_id=request.user_id,
        center_lat=request.latitude,
        center_lon=request.longitude,
        prompt=request.prompt,
        radius_km=request.radius_km,
        total_candidates=len(place_ids),
        total_ranked=len(recommendations),
        recommendations=recommendations,
        timestamp=datetime.utcnow().isoformat(),
    )
