from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


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


class AddLocationRequest(BaseModel):
    google_place_id: str = Field(..., description="Google Place ID for the location")
    classify_photo: Optional[bool] = Field(True, description="Whether to classify the location's photo with AI")
    generate_emoji: Optional[bool] = Field(True, description="Whether to generate an emoji for the location")


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
    emoji: Optional[str] = None


class MagicSearchRequest(BaseModel):
    user_id: str = Field(..., description="User identifier")
    latitude: float = Field(..., description="Center point latitude", ge=-90, le=90)
    longitude: float = Field(..., description="Center point longitude", ge=-180, le=180)
    prompt: str = Field(..., description="Free-text search prompt")
    radius_km: Optional[float] = Field(2.0, description="Search radius in kilometers", gt=0, le=50)
    max_results: Optional[int] = Field(20, description="Max number of places to search and rank", ge=1, le=50)
    include_taste_breakdown: Optional[bool] = Field(False, description="Include detailed taste score breakdown")


class MagicSearchResponse(BaseModel):
    user_id: str
    center_lat: float
    center_lon: float
    prompt: str
    radius_km: float
    total_candidates: int
    total_ranked: int
    recommendations: List[LocationRecommendation]
    timestamp: str
