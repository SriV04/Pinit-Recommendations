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
import os
import requests
import json
import logging
import base64
import re
from openai import OpenAI

from recommendation.proximal_recommendation import (
    build_proximal_recommendations,
    build_batch_proximal_recommendations,
    ProximalConfig,
    get_location_coordinates
)
from recommendation.tag_taxonomy import get_tags_dataframe
from recommendation.static_tagging import load_locations, load_reviews, build_location_tags
from recommendation.user_profiles import (
    ensure_user_actions, 
    build_user_tag_affinities,
    load_user_tag_affinities_from_supabase,
    convert_supabase_affinities_to_profile_format
)
from config import PipelineConfig, PipelinePaths, ReviewTagConfig
from supabase_client.supabase_service import get_supabase_service

# Configure logging (respect LOG_LEVEL env)
log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_name, logging.INFO)
logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Pydantic models for request/response

class TagMatch(BaseModel):
    tag: str = Field(..., description="Tag name")
    user_score: float = Field(..., description="User preference score (0-100)")
    location_score: float = Field(..., description="Location score for this tag (0-100)")
    contribution: float = Field(..., description="Contribution to taste score (0-100)")


class ProximalRequest(BaseModel):
    user_id: str = Field(..., description="User identifier")
    latitude: float = Field(..., description="Center point latitude", ge=-90, le=90)
    longitude: float = Field(..., description="Center point longitude", ge=-180, le=180)
    radius_km: Optional[float] = Field(2.0, description="Search radius in kilometers", gt=0, le=50)
    max_results: Optional[int] = Field(20, description="Maximum number of results", ge=1, le=100)
    taste_weight: Optional[float] = Field(0.2, description="Weight for taste matching", ge=0, le=1)
    proximity_weight: Optional[float] = Field(0.6, description="Weight for proximity", ge=0, le=1)
    quality_weight: Optional[float] = Field(0.2, description="Weight for quality", ge=0, le=1)
    include_taste_breakdown: Optional[bool] = Field(False, description="Include detailed taste score breakdown")


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
    taste_breakdown: Optional[List[TagMatch]] = Field(None, description="Breakdown of taste score by matching tags")


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


def get_taste_breakdown(location_id: int, user_id: str, user_tags: pd.DataFrame, 
                        location_tags: pd.DataFrame, top_n: int = 5) -> List[Dict]:
    """
    Get the breakdown of which tags contributed to the taste score.
    
    Returns:
        List of dicts with tag, user_score, location_score, contribution
    """
    user_profile = user_tags[user_tags['user_id'] == user_id]
    loc_tags = location_tags[location_tags['location_id'] == location_id]
    
    if user_profile.empty or loc_tags.empty:
        return []
    
    # Merge to find matching tags
    merged = user_profile.merge(
        loc_tags[['tag_id', 'tag_text', 'score']],
        on='tag_id',
        how='inner',
        suffixes=('_user', '_loc')
    )
    
    if merged.empty:
        return []
    
    # Calculate contribution
    merged['contribution'] = (merged['score_user'] / 100.0) * (merged['score_loc'] / 100.0)
    merged = merged.sort_values('contribution', ascending=False)
    
    results = []
    for _, row in merged.head(top_n).iterrows():
        # Use either tag_text_user or tag_text_loc (they should be the same)
        tag_name = row.get('tag_text_user', row.get('tag_text_loc', 'unknown'))
        results.append({
            'tag': tag_name,
            'user_score': float(row['score_user']),
            'location_score': float(row['score_loc']),
            'contribution': float(row['contribution'] * 100)  # Convert to 0-100 scale
        })
    
    return results


def load_data():
    """Load all necessary data for recommendations from Supabase."""
    global DATA_CACHE
    
    if DATA_CACHE["loaded"]:
        logger.info("Data already loaded, skipping reload")
        return
    
    logger.info("Starting to load recommendation data from Supabase...")
    
    # Get Supabase client
    supabase = get_supabase_service()
    
    # Load tags from Supabase
    logger.info("Loading tags from Supabase...")
    tags_df = get_tags_dataframe()
    logger.info(f"Loaded {len(tags_df)} tags")
    
    # Load locations from Supabase
    logger.info("Loading locations from Supabase...")
    # Fetch all locations (large upper bound)
    locations_data = supabase.get_locations(limit=1_000_000)
    if not locations_data:
        logger.error("No locations found in Supabase")
        raise ValueError("No locations found in Supabase database")
    
    locations = pd.DataFrame(locations_data)
    logger.info(f"Loaded {len(locations):,} locations")
    
    # Load location_tags from Supabase
    logger.info("Loading location tags from Supabase (bulk)...")
    location_tags_data = supabase.get_location_tags(limit=1_000_000)
    
    if not location_tags_data:
        logger.warning("No location tags found in Supabase")
        location_tags = pd.DataFrame(columns=['location_id', 'tag_id', 'score'])
    else:
        location_tags = pd.DataFrame(location_tags_data)
        # Add tag_text for easier lookup
        tag_id_to_text = tags_df.set_index('tag_id')['text'].to_dict()
        location_tags['tag_text'] = location_tags['tag_id'].map(tag_id_to_text)
    
    logger.info(f"Loaded {len(location_tags):,} location-tag associations")
    
    # Load user tag affinities from Supabase
    logger.info("Loading user tag affinities from Supabase...")
    supabase_affinities = load_user_tag_affinities_from_supabase()
    
    if supabase_affinities.empty:
        logger.error("No user tag affinities found in Supabase")
        raise ValueError("No user tag affinities found in Supabase. Please ensure users have profiles.")
    
    logger.info(f"Loaded {len(supabase_affinities):,} user tag affinities")
    user_tags = convert_supabase_affinities_to_profile_format(supabase_affinities, tags_df)
    
    # Build user history from affinity data
    user_history = (
        user_tags.groupby("user_id")
        .size()
        .reset_index(name="n_actions")
        .sort_values(by="n_actions", ascending=False)
    )
    logger.info(f"Found {len(user_tags['user_id'].unique())} users")
    
    # Cache data
    DATA_CACHE["locations"] = locations
    DATA_CACHE["tags"] = tags_df
    DATA_CACHE["location_tags"] = location_tags
    DATA_CACHE["user_tags"] = user_tags
    DATA_CACHE["user_history"] = user_history
    DATA_CACHE["loaded"] = True
    
    logger.info("Data ready for API requests")


@app.on_event("startup")
async def startup_event():
    """Load data when API starts."""
    logger.info("API startup initiated")
    try:
        load_data()
        logger.info("API startup completed successfully")
    except Exception as e:
        logger.error(f"Error during startup: {e}", exc_info=True)
        raise


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
    logger.info(f"Proximal recommendation request for user: {request.user_id}, location: ({request.latitude}, {request.longitude}), radius: {request.radius_km}km")
    
    if not DATA_CACHE["loaded"]:
        logger.warning("Recommendation request received but data not loaded yet")
        raise HTTPException(status_code=503, detail="Data still loading, please try again")
    
    # Validate user exists
    if request.user_id not in DATA_CACHE["user_tags"]["user_id"].values:
        logger.warning(f"User not found: {request.user_id}")
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
        logger.info(f"No recommendations found for user {request.user_id} within {request.radius_km}km radius")
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
        # Get taste breakdown if requested
        taste_breakdown = None
        if request.include_taste_breakdown:
            breakdown = get_taste_breakdown(
                location_id=int(row["location_id"]),
                user_id=request.user_id,
                user_tags=DATA_CACHE["user_tags"],
                location_tags=DATA_CACHE["location_tags"],
                top_n=5
            )
            if breakdown:
                taste_breakdown = [TagMatch(**item) for item in breakdown]
        
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
            rank=int(row["rank"]),
            taste_breakdown=taste_breakdown
        ))
    
    logger.info(f"Returning {len(recommendations)} recommendations for user {request.user_id}")
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


class AddLocationRequest(BaseModel):
    google_place_id: str = Field(..., description="Google Place ID for the location")
    classify_photo: Optional[bool] = Field(True, description="Whether to classify the location's photo with AI")


class AddLocationResponse(BaseModel):
    success: bool
    message: str
    location_id: Optional[int] = None
    google_place_id: str
    name: Optional[str] = None
    tags_count: Optional[int] = None
    already_existed: bool
    photo_reference: Optional[str] = None
    photo_score: Optional[int] = None


def get_photo_reference(place_id: str, api_key: str) -> Optional[str]:
    """Get the first photo resource name from Google Places API v1"""
    url = f"https://places.googleapis.com/v1/places/{place_id}"
    
    headers = {
        "Content-type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "id,displayName,photos"
    }
    
    try:
        res = requests.get(url, headers=headers, timeout=10).json()
        photos = res.get("photos", [])
        
        if not photos:
            return None
        
        # Extract photo resource name: "places/{place_id}/photos/{photo_id}"
        photo_name = photos[0].get("name")
        return photo_name
    except Exception as e:
        logger.warning(f"Error fetching photo reference for {place_id}: {e}")
        return None


def download_photo(photo_reference: str, api_key: str) -> Optional[bytes]:
    """Download image bytes from Google Places Photo API"""
    url = f'https://places.googleapis.com/v1/{photo_reference}/media?maxHeightPx=400&maxWidthPx=400&key={api_key}'
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.content
    except Exception as e:
        logger.warning(f"Error downloading photo: {e}")
        return None


def classify_image_with_openai(image_bytes: bytes) -> Optional[int]:
    """
    Send image to OpenAI vision model for classification.
    Returns a quality score (1-3) or None on error.
    """
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        logger.warning("OPENAI_API_KEY not found, skipping photo classification")
        return None
    
    try:
        client = OpenAI(api_key=openai_key)
        base64_img = base64.b64encode(image_bytes).decode("utf-8")
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_img}"
                            }
                        },
                        {
                            "type": "text",
                            "text": """You are a restaurant photo quality classifier. Evaluate this restaurant photo and assign a quality score:

- **3 (High Quality)**: Professional photography that represents the restaurant well
- **2 (Acceptable)**: Decent photo with some amateur qualities, but still usable
- **1 (Low Quality)**: Amateur smartphone photo that appears unprofessional

Evaluate based on:
1. Image clarity and resolution
2. Photography quality (professional vs smartphone)
3. Composition (angle, framing, completeness)
4. Lighting (natural, balanced, appropriate)
5. Staging and presentation

Be lenient - most professional photos should get a 3. Only penalize clearly amateur or poorly executed photos.

Provide your response as exactly:
Score: [1, 2, or 3]"""
                        }
                    ]
                },
            ],
            max_tokens=500
        )
        
        result_text = response.choices[0].message.content
        score_match = re.search(r'Score:\s*(\d)', result_text)
        
        if score_match:
            score = int(score_match.group(1))
            logger.info(f"Photo classified with score: {score}")
            return score
        else:
            logger.warning(f"Could not parse score from OpenAI response: {result_text}")
            return None
    except Exception as e:
        logger.error(f"Error classifying image with OpenAI: {e}", exc_info=True)
        return None


def classify_location_photo(google_place_id: str, api_key: str) -> tuple[Optional[str], Optional[int]]:
    """
    Fetch and classify the primary photo for a location.
    Returns (photo_reference, photo_score) tuple.
    """
    logger.info(f"Starting photo classification for place ID: {google_place_id}")
    
    # Get photo reference
    photo_ref = get_photo_reference(google_place_id, api_key)
    if not photo_ref:
        logger.info("No photo found for location")
        return None, None
    
    logger.info(f"Found photo reference: {photo_ref}")
    
    # Download photo
    image_bytes = download_photo(photo_ref, api_key)
    if not image_bytes:
        logger.warning("Failed to download photo")
        return photo_ref, None
    
    logger.info(f"Downloaded photo ({len(image_bytes)} bytes)")
    
    # Classify with OpenAI
    score = classify_image_with_openai(image_bytes)
    
    return photo_ref, score


def fetch_google_place_details(google_place_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch place details from Google Places API.
    """
    logger.info(f"Fetching details for Google Place ID: {google_place_id}")
    
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        logger.error("GOOGLE_MAPS_API_KEY not found in environment variables")
        raise ValueError("GOOGLE_MAPS_API_KEY not found in environment variables")
    
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    fields = ",".join([
        "place_id", "name", "types", "rating", "price_level",
        "user_ratings_total", "editorial_summary", "opening_hours",
        "international_phone_number", "website", "review",
        "geometry", "vicinity", "business_status"
    ])
    
    params = {
        "key": api_key,
        "place_id": google_place_id,
        "fields": fields
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get("status") != "OK":
            logger.warning(f"Google Places API returned status: {data.get('status')} for place ID: {google_place_id}")
            return None
        
        logger.info(f"Successfully fetched details for place: {data.get('result', {}).get('name', 'Unknown')}")
        return data.get("result", {})
    except Exception as e:
        logger.error(f"Error fetching Google Place details for {google_place_id}: {e}", exc_info=True)
        return None


def process_and_tag_location(place_details: Dict[str, Any], supabase) -> tuple[int, int]:
    """
    Process place details, create location entry, and generate tags.
    Returns (location_id, tags_count).
    """
    place_name = place_details.get("name", "Unknown")
    logger.info(f"Processing location: {place_name}")
    
    # Extract data from place details
    geometry = place_details.get("geometry", {})
    location = geometry.get("location", {})
    opening_hours = place_details.get("opening_hours", {})
    reviews = place_details.get("reviews", [])
    
    logger.debug(f"Extracted {len(reviews)} reviews for {place_name}")
    
    # Prepare location data
    location_data = {
        "name": place_details.get("name", ""),
        "google_place_id": place_details.get("place_id"),
        "vicinity": place_details.get("vicinity"),
        "lat": location.get("lat"),
        "lng": location.get("lng"),
        "rating": place_details.get("rating"),
        "user_ratings_total": place_details.get("user_ratings_total"),
        "price_level": place_details.get("price_level"),
        "business_status": place_details.get("business_status"),
        "editorial_summary": place_details.get("editorial_summary", {}).get("overview"),
        "website": place_details.get("website"),
        "international_phone_number": place_details.get("international_phone_number"),
        "types": ",".join(place_details.get("types", [])),
        "opening_hours_text": opening_hours.get("weekday_text", []),
        "opening_hours_periods": json.dumps(opening_hours.get("periods", [])) if opening_hours.get("periods") else None,
        "open_now": opening_hours.get("open_now"),
    }
    
    # Create location in database
    logger.info(f"Creating location in database: {place_name}")
    created_location = supabase.create_location(**location_data)
    location_id = created_location["location_id"]
    logger.info(f"Location created with ID: {location_id}")
    
    # Process reviews for tagging
    reviews_data = []
    for review in reviews:
        reviews_data.append({
            "location_id": location_id,
            "author_name": review.get("author_name", "anon"),
            "language": review.get("language", ""),
            "text": review.get("text", ""),
            "rating": review.get("rating"),
            "time": review.get("time")
        })
    
    # Create DataFrames for tagging
    # Parse types list from comma-separated string
    types_list = location_data["types"].split(",") if location_data["types"] else []
    types_list = [t.strip() for t in types_list if t.strip()]
    
    # Determine cuisine from types
    cuisine = place_details.get("types", ["unknown"])[0] if place_details.get("types") else "unknown"
    
    # Parse opening hours to determine schedule flags
    opening_hours_json = location_data.get("opening_hours_periods")
    is_open_late = False
    is_open_early = False
    is_sunday_open = False
    
    if opening_hours_json:
        try:
            periods = json.loads(opening_hours_json) if isinstance(opening_hours_json, str) else opening_hours_json
            for period in periods:
                open_time = period.get("open", {})
                close_time = period.get("close", {})
                open_day = open_time.get("day")
                close_day = close_time.get("day")
                open_hour = open_time.get("time", "0000")
                close_hour = close_time.get("time", "0000")
                
                # Convert to minutes
                if open_hour and len(str(open_hour)) >= 4:
                    open_minutes = int(str(open_hour)[:2]) * 60 + int(str(open_hour)[2:4])
                    if open_minutes < 8 * 60:  # Opens before 8am
                        is_open_early = True
                
                if close_hour and len(str(close_hour)) >= 4:
                    close_minutes = int(str(close_hour)[:2]) * 60 + int(str(close_hour)[2:4])
                    if close_minutes >= 23 * 60:  # Closes after 11pm
                        is_open_late = True
                
                if open_day == 0 or close_day == 0:  # Sunday
                    is_sunday_open = True
        except Exception as e:
            logger.warning(f"Could not parse opening hours for location {location_id}: {e}")
    
    # Determine price bucket
    price_level_val = location_data.get("price_level")
    if price_level_val is None or (isinstance(price_level_val, float) and pd.isna(price_level_val)):
        price_bucket = "unknown"
    elif price_level_val <= 1:
        price_bucket = "value"
    elif price_level_val == 2:
        price_bucket = "mid"
    elif price_level_val >= 3:
        price_bucket = "premium"
    else:
        price_bucket = "unknown"
    
    # Calculate derived scores (similar to load_locations)
    import numpy as np
    user_ratings = location_data.get("user_ratings_total") or 0
    log_reviews = np.log1p(user_ratings)
    rating_val = location_data.get("rating")
    rating_normalized = rating_val if rating_val is not None else 0.0
    
    location_df = pd.DataFrame([{
        "location_id": location_id,
        "name": location_data["name"],
        "types": location_data["types"],
        "cuisine_primary": cuisine,
        "price_level": location_data["price_level"],
        "price_bucket": price_bucket,
        "rating": location_data["rating"],
        "user_ratings_total": location_data["user_ratings_total"],
        "opening_hours_periods": location_data["opening_hours_periods"],
        "is_open_late": is_open_late,
        "is_open_early": is_open_early,
        "is_sunday_open": is_sunday_open,
        "log_reviews": log_reviews,
        "popularity_score": 0.5,  # Default mid-range since we can't compare to others
        "expected_popularity": log_reviews,  # Use same as actual since no comparison
        "residual_popularity": 0.0,  # No residual without comparison
        "quality_score": rating_normalized / 5.0 if rating_normalized > 0 else 0.5,  # Normalize to 0-1
        "hidden_gem_score": 0.5,  # Default neutral score for new locations
    }])
    
    # Add types_list as a column (list type)
    location_df["types_list"] = [types_list]
    
    reviews_df = pd.DataFrame(reviews_data) if reviews_data else pd.DataFrame(columns=["location_id", "author_name", "language", "text"])
    
    # Build tags
    logger.info(f"Building tags for location {location_id}")
    review_cfg = ReviewTagConfig(min_unique_authors=1, min_mentions=1)  # Lower thresholds for single location
    location_tags_df = build_location_tags(location_df, reviews_df, review_cfg)
    logger.info(f"Generated {len(location_tags_df)} tags for location {location_id}")
    
    # Save tags to database
    tags_count = 0
    if not location_tags_df.empty:
        for _, tag_row in location_tags_df.iterrows():
            try:
                supabase.create_location_tag(
                    location_id=int(tag_row["location_id"]),
                    tag_id=str(tag_row["tag_id"]),
                    score=float(tag_row["score"]),
                    source=tag_row.get("source", "api"),
                    metadata=json.loads(tag_row["metadata"]) if isinstance(tag_row.get("metadata"), str) else tag_row.get("metadata")
                )
                tags_count += 1
            except Exception as e:
                logger.warning(f"Could not save tag {tag_row.get('tag_text')} for location {location_id}: {e}")
    
    logger.info(f"Successfully saved {tags_count} tags for location {location_id}")
    return location_id, tags_count


@app.post("/locations/add", response_model=AddLocationResponse)
async def add_location_by_place_id(request: AddLocationRequest):
    """
    Add a new location to the database by Google Place ID.
    
    This endpoint will:
    1. Check if the location already exists in the database
    2. If not, fetch details from Google Places API
    3. Process and tag the location
    4. Store it in the database
    """
    logger.info(f"Received request to add location with Google Place ID: {request.google_place_id}")
    
    supabase = get_supabase_service()
    
    # Step 1: Check if location already exists
    logger.info(f"Checking if location already exists: {request.google_place_id}")
    existing_location = supabase.get_location_by_google_place_id(request.google_place_id)
    
    if existing_location:
        logger.info(f"Location already exists: {existing_location.get('name')} (ID: {existing_location['location_id']})")
        return AddLocationResponse(
            success=True,
            message="Location already exists in database",
            location_id=existing_location["location_id"],
            google_place_id=request.google_place_id,
            name=existing_location.get("name"),
            tags_count=None,
            already_existed=True
        )
    
    # Step 2: Fetch from Google Places API
    place_details = fetch_google_place_details(request.google_place_id)
    
    if not place_details:
        logger.error(f"Failed to fetch details for Google Place ID: {request.google_place_id}")
        raise HTTPException(
            status_code=404,
            detail=f"Could not fetch details for Google Place ID: {request.google_place_id}"
        )
    
    # Verify it's a restaurant/food establishment
    types = place_details.get("types", [])
    food_types = {"restaurant", "cafe", "bar", "food", "meal_delivery", "meal_takeaway"}
    if not any(t in food_types for t in types):
        logger.warning(f"Rejected non-food establishment: {place_details.get('name')} (types: {types})")
        raise HTTPException(
            status_code=400,
            detail=f"Location is not a restaurant or food establishment. Types: {types}"
        )
    
    # Step 3: Process and tag the location
    try:
        location_id, tags_count = process_and_tag_location(place_details, supabase)
        
        # Step 4: Classify photo if requested
        photo_ref = None
        photo_score = None
        
        if request.classify_photo:
            logger.info("Starting photo classification...")
            api_key = os.getenv("GOOGLE_MAPS_API_KEY")
            photo_ref, photo_score = classify_location_photo(request.google_place_id, api_key)
            
            # Update location with photo data
            if photo_ref or photo_score:
                update_data = {}
                if photo_ref:
                    update_data["photo_reference"] = photo_ref
                if photo_score:
                    update_data["photo_reference_score"] = photo_score
                
                if update_data:
                    supabase.update_location(location_id, **update_data)
                    logger.info(f"Updated location with photo data: ref={bool(photo_ref)}, score={photo_score}")
        
        logger.info(f"Successfully added location: {place_details.get('name')} (ID: {location_id}, Tags: {tags_count}, Photo Score: {photo_score})")
        return AddLocationResponse(
            success=True,
            message="Location successfully added and tagged",
            location_id=location_id,
            google_place_id=request.google_place_id,
            name=place_details.get("name"),
            tags_count=tags_count,
            already_existed=False,
            photo_reference=photo_ref,
            photo_score=photo_score
        )
    except Exception as e:
        logger.error(f"Error processing location {request.google_place_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error processing location: {str(e)}"
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
