from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

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

    # Five-component weights (adaptive rebalancing may override these)
    quality_weight: Optional[float] = Field(0.30, description="Weight for quality (ratings)", ge=0, le=1)
    vibe_weight: Optional[float] = Field(0.25, description="Weight for vibe matching", ge=0, le=1)
    dietary_weight: Optional[float] = Field(0.10, description="Weight for dietary matching", ge=0, le=1)
    social_weight: Optional[float] = Field(0.20, description="Weight for friend-based social score", ge=0, le=1)
    collaborative_weight: Optional[float] = Field(0.15, description="Weight for collaborative filtering", ge=0, le=1)

    include_taste_breakdown: Optional[bool] = Field(False, description="Include detailed taste score breakdown")

    # Optional filters (applied with score threshold)
    filters: Optional[FilterOptions] = Field(
        None,
        description="Optional filters for cuisine (OR logic) and vibe (AND logic)"
    )


class BatchProximalRequest(BaseModel):
    user_ids: List[str] = Field(..., description="List of user identifiers")
    latitude: float = Field(..., description="Center point latitude", ge=-90, le=90)
    longitude: float = Field(..., description="Center point longitude", ge=-180, le=180)
    radius_km: Optional[float] = Field(2.0, description="Search radius in kilometers", gt=0, le=50)
    max_results: Optional[int] = Field(20, description="Maximum number of results per user", ge=1, le=100)


class FriendSave(BaseModel):
    """A friend's interaction with a location that contributed to its social score."""
    friend_id: str
    friend_name: str
    friend_username: Optional[str] = None
    friend_profile_image_url: Optional[str] = None
    action_type: str = Field(..., description="Action type: save, been_to, like, bubble_save, shared_video, dislike")
    rating: Optional[int] = Field(None, description="Rating value (unused — retained for backwards compatibility)")
    timestamp: str


class MomentumIndicator(BaseModel):
    """Signals whether a location is trending or newly discovered."""
    trending: bool = Field(False, description="Location has rising saves/reviews recently")
    momentum_score: float = Field(0.0, description="Momentum score based on recent activity velocity")
    new_discovery: bool = Field(False, description="Recently added location with limited data")


class LocationRecommendation(BaseModel):
    location_id: int
    name: str
    vicinity: Optional[str] = None
    cuisine_primary: Optional[str] = None
    rating: Optional[float] = None
    user_ratings_total: Optional[int] = None
    price_level: Optional[float] = None
    distance_km: float

    # Five-component scores
    vibe_score: float = Field(..., description="Vibe match score (0-1)")
    dietary_score: float = Field(..., description="Dietary match score (0-1)")
    quality_score: float = Field(..., description="Quality score (0-1)")
    social_score: float = Field(0.0, description="Friend-based social score (0-1)")
    collaborative_score: float = Field(0.0, description="Collaborative filtering score (0-1)")

    final_score: float
    rank: int
    taste_breakdown: Optional[List[TagMatch]] = Field(None, description="Breakdown of taste score by matching tags")
    friend_saves: Optional[List[FriendSave]] = Field(None, description="Friends who saved/visited this location")
    momentum: Optional[MomentumIndicator] = Field(None, description="Trending and discovery indicators")


class ProximalResponse(BaseModel):
    user_id: str
    center_lat: float
    center_lon: float
    radius_km: float
    total_results: int
    recommendations: List[LocationRecommendation]
    weights_used: Optional[Dict[str, float]] = Field(None, description="Actual weights used after adaptive rebalancing")
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

class FilterOptions(BaseModel):
    cuisine: Optional[List[str]] = Field(
        None,
        description="Cuisine IDs (OR logic - matches one of them )",
        example=["italian", "japanese", "mexican"]
    )
    vibe: Optional[List[str]] = Field(
        None,
        description="Required vibe tag IDs (AND logic - must have all)",
        example=["outdoor-seating", "dog-friendly"]
    )


class BubbleRequest(BaseModel):
    """Request for group (bubble) recommendations."""
    user_ids: List[str] = Field(
        ...,
        description="List of user IDs in the group",
        min_items=2,
        example=["alice", "bob", "charlie"]
    )
    bubble_id: Optional[str] = Field(
        None,
        description=(
            "Optional bubble UUID. When provided, locations the group has "
            "added to bubble_locations are used to bias each user's effective "
            "vibe vector toward their current intent for this bubble, and "
            "those locations get a small ranking boost."
        ),
        example="d4f3e2c1-b6a7-4d5f-8e9a-0b1c2d3e4f5a",
    )
    latitude: float = Field(..., description="Center point latitude", ge=-90, le=90)
    longitude: float = Field(..., description="Center point longitude", ge=-180, le=180)
    radius_km: Optional[float] = Field(2.0, description="Search radius in kilometers", gt=0, le=50)
    max_results: Optional[int] = Field(20, description="Maximum results", ge=1, le=100)

    vibe_weight: Optional[float] = Field(0.34, description="Weight for group vibe matching", ge=0, le=1)
    dietary_weight: Optional[float] = Field(0.33, description="Weight for dietary matching", ge=0, le=1)
    quality_weight: Optional[float] = Field(0.33, description="Weight for quality (ratings)", ge=0, le=1)

    # Optional features
    include_individual_scores: Optional[bool] = Field(
        False,
        description="Include each user's individual scores for transparency"
    )
    include_vibe_breakdown: Optional[bool] = Field(
        False,
        description="Include breakdown of vibe score contributions"
    )

    # Optional filters (applied with score >= 50 threshold)
    filters: Optional[FilterOptions] = Field(
        None,
        description="Optional filters for cuisine (OR logic) and vibe (AND logic)"
    )


class UserVibeScore(BaseModel):
    """One user's individual vibe match score for a location."""
    user_id: str
    vibe_score: float = Field(..., description="Centered cosine similarity in [0, 1]")


class IndividualScore(BaseModel):
    """Individual user's score for a location."""
    user_id: str
    vibe_score: float = Field(..., description="Vibe match score")
    dietary_score: float = Field(..., description="Dietary match score")
    activity_weight: float = Field(..., description="User's activity weight in aggregation")


class BubbleLocationRecommendation(BaseModel):
    """Location recommendation for a group."""
    # Basic location info
    location_id: int
    name: str
    vicinity: Optional[str] = None
    cuisine_primary: Optional[str] = None
    rating: Optional[float] = None
    user_ratings_total: Optional[int] = None
    price_level: Optional[float] = None
    distance_km: float

    # Group scores
    group_vibe_score: float = Field(..., description="Aggregated group vibe score (context-aware)")
    group_dietary_score: float = Field(..., description="Aggregated group dietary score (MAX pooled)")
    quality_score: float
    final_score: float
    rank: int

    # Per-user vibe match for this location (always returned). Each user
    # is scored with their *effective* vibe vector — that is, their personal
    # vibe_tag_affinity blended with what they've added to this bubble.
    individual_vibe_scores: List[UserVibeScore] = Field(
        default_factory=list,
        description="Per-user vibe match score for this location",
    )
    is_in_bubble: bool = Field(
        False,
        description="True if this location is already in the bubble's saved list",
    )

    # Transparency metrics (optional)
    individual_scores: Optional[List[IndividualScore]] = Field(
        None,
        description="Each user's individual scores (if include_individual_scores=true)"
    )
    min_individual_score: Optional[float] = Field(
        None,
        description="Lowest individual taste score (fairness metric)"
    )
    max_individual_score: Optional[float] = Field(
        None,
        description="Highest individual taste score"
    )
    score_variance: Optional[float] = Field(
        None,
        description="Variance in individual scores (measures disagreement)"
    )
    vibe_breakdown: Optional[List[TagMatch]] = Field(
        None,
        description="Breakdown of vibe score by tag (if include_vibe_breakdown=true)"
    )


class BubbleResponse(BaseModel):
    """Response for group (bubble) recommendations."""
    # Group info
    user_ids: List[str]
    group_size: int

    # Search parameters
    center_lat: float
    center_lon: float
    radius_km: float

    # Results
    total_results: int
    recommendations: List[BubbleLocationRecommendation]

    # Filtering metadata
    optional_filters_applied: Optional[FilterOptions] = Field(
        None,
        description="Optional filters that were applied (if any)"
    )
    locations_before_filtering: int = Field(
        ...,
        description="Number of locations before filter constraints"
    )
    locations_after_filtering: int = Field(
        ...,
        description="Number of locations after all filters applied"
    )

    timestamp: str
