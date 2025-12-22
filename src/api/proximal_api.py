"""
FastAPI service for proximal (location-based) recommendations.
Exposes REST endpoints for getting personalized recommendations within a radius.
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from pathlib import Path
import pandas as pd
from datetime import datetime

from recommendation.proximal_recommendation import (
    build_proximal_recommendations,
    build_batch_proximal_recommendations,
    ProximalConfig,
    get_location_coordinates
)
from recommendation.tag_taxonomy import get_tags_dataframe
from recommendation.static_tagging import load_locations, load_reviews, build_location_tags
from recommendation.user_profiles import ensure_user_actions, build_user_tag_affinities
from config import PipelineConfig, PipelinePaths, ReviewTagConfig


# Pydantic models for request/response
class ProximalRequest(BaseModel):
    user_id: str = Field(..., description="User identifier")
    latitude: float = Field(..., description="Center point latitude", ge=-90, le=90)
    longitude: float = Field(..., description="Center point longitude", ge=-180, le=180)
    radius_km: Optional[float] = Field(2.0, description="Search radius in kilometers", gt=0, le=50)
    max_results: Optional[int] = Field(20, description="Maximum number of results", ge=1, le=100)
    taste_weight: Optional[float] = Field(0.2, description="Weight for taste matching", ge=0, le=1)
    proximity_weight: Optional[float] = Field(0.6, description="Weight for proximity", ge=0, le=1)
    quality_weight: Optional[float] = Field(0.2, description="Weight for quality", ge=0, le=1)


class BatchProximalRequest(BaseModel):
    user_ids: List[str] = Field(..., description="List of user identifiers")
    latitude: float = Field(..., description="Center point latitude", ge=-90, le=90)
    longitude: float = Field(..., description="Center point longitude", ge=-180, le=180)
    radius_km: Optional[float] = Field(2.0, description="Search radius in kilometers", gt=0, le=50)
    max_results: Optional[int] = Field(20, description="Maximum number of results per user", ge=1, le=100)


class LocationRecommendation(BaseModel):
    location_id: int
    name: str
    vicinity: Optional[str] = None
    cuisine_primary: Optional[str] = None
    rating: Optional[float] = None
    user_ratings_total: Optional[int] = None
    price_level: Optional[float] = None
    distance_km: float
    taste_score: float
    proximity_score: float
    quality_score: float
    final_score: float
    rank: int


class ProximalResponse(BaseModel):
    user_id: str
    center_lat: float
    center_lon: float
    radius_km: float
    total_results: int
    recommendations: List[LocationRecommendation]
    timestamp: str


class BatchProximalResponse(BaseModel):
    center_lat: float
    center_lon: float
    radius_km: float
    total_users: int
    total_recommendations: int
    results: List[ProximalResponse]
    timestamp: str


class LocationCoordinatesResponse(BaseModel):
    location_id: int
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    found: bool


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    data_loaded: bool
    total_locations: int
    total_users: int
    total_tags: int


# Initialize FastAPI app
app = FastAPI(
    title="Pinit Proximal Recommendations API",
    description="Location-based personalized restaurant recommendations",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global data storage (loaded on startup)
DATA_CACHE = {
    "locations": None,
    "tags": None,
    "location_tags": None,
    "user_tags": None,
    "user_history": None,
    "loaded": False
}


def load_data():
    """Load all necessary data for recommendations."""
    global DATA_CACHE
    
    if DATA_CACHE["loaded"]:
        return
    
    print("Loading recommendation data...")
    
    # Configuration
    DATA_DIR = Path("data/raw")
    CITY_NAME = "london"
    OUTPUT_DIR = Path("output/api_cache")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    paths = PipelinePaths(data_dir=DATA_DIR, city_name=CITY_NAME, output_dir=OUTPUT_DIR)
    review_cfg = ReviewTagConfig(min_unique_authors=2, min_mentions=3)
    config = PipelineConfig(paths=paths, review_tagging=review_cfg, synthetic_users=True)
    
    # Load data
    tags_df = get_tags_dataframe()
    locations = load_locations(paths)
    place_lookup = locations.set_index("google_place_id")["location_id"].to_dict()
    reviews = load_reviews(paths, place_lookup)
    location_tags = build_location_tags(locations, reviews, config.review_tagging)
    user_actions, synthetic = ensure_user_actions(paths, locations, location_tags, allow_synthetic=True)
    user_tags, user_history = build_user_tag_affinities(user_actions, location_tags, locations)
    
    # Cache data
    DATA_CACHE["locations"] = locations
    DATA_CACHE["tags"] = tags_df
    DATA_CACHE["location_tags"] = location_tags
    DATA_CACHE["user_tags"] = user_tags
    DATA_CACHE["user_history"] = user_history
    DATA_CACHE["loaded"] = True
    
    print(f"✓ Loaded {len(locations):,} locations")
    print(f"✓ Loaded {len(tags_df)} tags")
    print(f"✓ Loaded {len(user_tags):,} user-tag affinities")
    print(f"✓ Data ready for API requests")


@app.on_event("startup")
async def startup_event():
    """Load data when API starts."""
    load_data()


@app.get("/", response_model=Dict[str, str])
async def root():
    """Root endpoint."""
    return {
        "message": "Pinit Proximal Recommendations API",
        "version": "1.0.0",
        "docs": "/docs"
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    return HealthResponse(
        status="healthy" if DATA_CACHE["loaded"] else "loading",
        timestamp=datetime.utcnow().isoformat(),
        data_loaded=DATA_CACHE["loaded"],
        total_locations=len(DATA_CACHE["locations"]) if DATA_CACHE["loaded"] else 0,
        total_users=DATA_CACHE["user_tags"]["user_id"].nunique() if DATA_CACHE["loaded"] else 0,
        total_tags=len(DATA_CACHE["tags"]) if DATA_CACHE["loaded"] else 0
    )


@app.post("/recommendations/proximal", response_model=ProximalResponse)
async def get_proximal_recommendations(request: ProximalRequest):
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
            detail=f"User '{request.user_id}' not found. Available users: {DATA_CACHE['user_tags']['user_id'].unique().tolist()[:5]}"
        )
    
    # Create config
    config = ProximalConfig(
        radius_km=request.radius_km,
        max_results=request.max_results,
        taste_weight=request.taste_weight,
        proximity_weight=request.proximity_weight,
        quality_weight=request.quality_weight
    )
    
    # Generate recommendations
    recs = build_proximal_recommendations(
        request.user_id,
        request.latitude,
        request.longitude,
        DATA_CACHE["locations"],
        DATA_CACHE["user_tags"],
        DATA_CACHE["location_tags"],
        config
    )
    
    if recs.empty:
        return ProximalResponse(
            user_id=request.user_id,
            center_lat=request.latitude,
            center_lon=request.longitude,
            radius_km=request.radius_km,
            total_results=0,
            recommendations=[],
            timestamp=datetime.utcnow().isoformat()
        )
    
    # Convert to response model
    recommendations = []
    for _, row in recs.iterrows():
        recommendations.append(LocationRecommendation(
            location_id=int(row["location_id"]),
            name=row["name"],
            vicinity=row.get("vicinity"),
            cuisine_primary=row.get("cuisine_primary"),
            rating=float(row["rating"]) if pd.notna(row.get("rating")) else None,
            user_ratings_total=int(row["user_ratings_total"]) if pd.notna(row.get("user_ratings_total")) else None,
            price_level=float(row["price_level"]) if pd.notna(row.get("price_level")) else None,
            distance_km=float(row["distance_km"]),
            taste_score=float(row["taste_score"]),
            proximity_score=float(row["proximity_score"]),
            quality_score=float(row["quality_score"]),
            final_score=float(row["final_score"]),
            rank=int(row["rank"])
        ))
    
    return ProximalResponse(
        user_id=request.user_id,
        center_lat=request.latitude,
        center_lon=request.longitude,
        radius_km=request.radius_km,
        total_results=len(recommendations),
        recommendations=recommendations,
        timestamp=datetime.utcnow().isoformat()
    )


@app.post("/recommendations/proximal/batch", response_model=BatchProximalResponse)
async def get_batch_proximal_recommendations(request: BatchProximalRequest):
    """
    Get proximal recommendations for multiple users at once.
    """
    if not DATA_CACHE["loaded"]:
        raise HTTPException(status_code=503, detail="Data still loading, please try again")
    
    # Validate users exist
    available_users = set(DATA_CACHE["user_tags"]["user_id"].unique())
    invalid_users = [uid for uid in request.user_ids if uid not in available_users]
    
    if invalid_users:
        raise HTTPException(
            status_code=404,
            detail=f"Users not found: {invalid_users[:5]}"
        )
    
    # Create config
    config = ProximalConfig(
        radius_km=request.radius_km,
        max_results=request.max_results
    )
    
    # Generate batch recommendations
    batch_recs = build_batch_proximal_recommendations(
        request.user_ids,
        request.latitude,
        request.longitude,
        DATA_CACHE["locations"],
        DATA_CACHE["user_tags"],
        DATA_CACHE["location_tags"],
        config
    )
    
    # Group by user
    results = []
    for user_id in request.user_ids:
        user_recs = batch_recs[batch_recs["user_id"] == user_id]
        
        recommendations = []
        for _, row in user_recs.iterrows():
            recommendations.append(LocationRecommendation(
                location_id=int(row["location_id"]),
                name=row["name"],
                vicinity=row.get("vicinity"),
                cuisine_primary=row.get("cuisine_primary"),
                rating=float(row["rating"]) if pd.notna(row.get("rating")) else None,
                user_ratings_total=int(row["user_ratings_total"]) if pd.notna(row.get("user_ratings_total")) else None,
                price_level=float(row["price_level"]) if pd.notna(row.get("price_level")) else None,
                distance_km=float(row["distance_km"]),
                taste_score=float(row["taste_score"]),
                proximity_score=float(row["proximity_score"]),
                quality_score=float(row["quality_score"]),
                final_score=float(row["final_score"]),
                rank=int(row["rank"])
            ))
        
        results.append(ProximalResponse(
            user_id=user_id,
            center_lat=request.latitude,
            center_lon=request.longitude,
            radius_km=request.radius_km,
            total_results=len(recommendations),
            recommendations=recommendations,
            timestamp=datetime.utcnow().isoformat()
        ))
    
    return BatchProximalResponse(
        center_lat=request.latitude,
        center_lon=request.longitude,
        radius_km=request.radius_km,
        total_users=len(request.user_ids),
        total_recommendations=len(batch_recs) if not batch_recs.empty else 0,
        results=results,
        timestamp=datetime.utcnow().isoformat()
    )


@app.get("/locations/{location_id}/coordinates", response_model=LocationCoordinatesResponse)
async def get_coordinates(location_id: int):
    """
    Get coordinates for a specific location.
    """
    if not DATA_CACHE["loaded"]:
        raise HTTPException(status_code=503, detail="Data still loading, please try again")
    
    coords = get_location_coordinates(location_id, DATA_CACHE["locations"])
    
    if coords is None:
        return LocationCoordinatesResponse(
            location_id=location_id,
            latitude=None,
            longitude=None,
            found=False
        )
    
    lat, lon = coords
    return LocationCoordinatesResponse(
        location_id=location_id,
        latitude=lat,
        longitude=lon,
        found=True
    )


@app.get("/users", response_model=Dict[str, Any])
async def list_users(limit: int = Query(10, ge=1, le=100)):
    """
    List available users in the system.
    """
    if not DATA_CACHE["loaded"]:
        raise HTTPException(status_code=503, detail="Data still loading, please try again")
    
    users = DATA_CACHE["user_tags"]["user_id"].unique().tolist()[:limit]
    
    return {
        "total_users": DATA_CACHE["user_tags"]["user_id"].nunique(),
        "users": users,
        "limit": limit
    }


@app.get("/users/{user_id}/profile", response_model=Dict[str, Any])
async def get_user_profile(user_id: str, top_n: int = Query(10, ge=1, le=50)):
    """
    Get a user's taste profile (top tag preferences).
    """
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
        preferences.append({
            "tag": tag_name,
            "score": float(row["score"])
        })
    
    return {
        "user_id": user_id,
        "top_preferences": preferences,
        "total_tags": len(user_profile)
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
