from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from pinit.api.schemas_magic import MagicAIEnrichmentPayload, MagicSearchDebug


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
    google_place_id: Optional[str] = None
    is_known_location: Optional[bool] = None
    name: str
    vicinity: Optional[str] = None
    cuisine_primary: Optional[str] = None
    rating: Optional[float] = None
    user_ratings_total: Optional[int] = None
    price_level: Optional[float] = None
    photo_reference: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    types: Optional[str] = None
    open_now: Optional[bool] = None
    distance_km: float

    # Rich place metadata. Populated from the locations table for known
    # locations, and directly from Google Places (Text Search) for external
    # candidates surfaced by /locations/magic-search. Optional everywhere
    # else so existing /recommendations/proximal callers stay unchanged.
    formatted_address: Optional[str] = None
    business_status: Optional[str] = None
    google_maps_uri: Optional[str] = None
    website: Optional[str] = None
    international_phone_number: Optional[str] = None
    editorial_summary: Optional[str] = None
    review_summary: Optional[str] = None
    opening_hours_text: Optional[List[str]] = None
    good_for_children: Optional[bool] = None
    good_for_groups: Optional[bool] = None
    good_for_watching_sports: Optional[bool] = None
    live_music: Optional[bool] = None
    outdoor_seating: Optional[bool] = None
    serves_beer: Optional[bool] = None
    serves_breakfast: Optional[bool] = None
    serves_brunch: Optional[bool] = None
    serves_cocktails: Optional[bool] = None
    serves_coffee: Optional[bool] = None
    serves_dessert: Optional[bool] = None
    serves_dinner: Optional[bool] = None
    serves_lunch: Optional[bool] = None
    serves_vegetarian_food: Optional[bool] = None
    serves_wine: Optional[bool] = None

    # Five-component scores
    vibe_score: float = Field(..., description="Vibe match score (0-1)")
    dietary_score: float = Field(..., description="Dietary match score (0-1)")
    quality_score: float = Field(..., description="Quality score (0-1)")
    social_score: float = Field(0.0, description="Friend-based social score (0-1)")
    collaborative_score: float = Field(0.0, description="Collaborative filtering score (0-1)")

    final_score: float
    rank: int

    # Photo metadata surfaced from get_locations_with_quality. `photos` itself
    # (the resource-name array) is intentionally not returned by the bulk
    # ranking RPC — a separate endpoint hydrates it on demand. The three
    # booleans here are enough for the client's cache-hit contract.
    image_stored: Optional[bool] = Field(None, description="True if a primary photo is uploaded to storage")
    image_unavailable: Optional[bool] = Field(None, description="True if photo sourcing has been tried and failed")
    extra_photos_stored: Optional[int] = Field(None, description="Count of extra photos uploaded beyond the primary")

    taste_breakdown: Optional[List[TagMatch]] = Field(None, description="Breakdown of taste score by matching tags")
    friend_saves: Optional[List[FriendSave]] = Field(None, description="Friends who saved/visited this location")
    momentum: Optional[MomentumIndicator] = Field(None, description="Trending and discovery indicators")


class MagicLocationRecommendation(LocationRecommendation):
    source: List[str] = Field(default_factory=list)
    source_metadata: List[Dict[str, Any]] = Field(default_factory=list)
    match_reasons: List[str] = Field(default_factory=list)
    intent_matches: Dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    section: Optional[str] = Field(
        None,
        description="Header of the response section this place is grouped under",
    )


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


class VideoInsightsPayload(BaseModel):
    """Video insights extracted by the TikTok processor, passed inline to /locations/add."""
    source_video_url: str
    key_dishes: Optional[List[Any]] = Field(default_factory=list)
    special_offers: Optional[List[Any]] = Field(default_factory=list)
    creator_notes: Optional[str] = None
    vibe_signals: Optional[Dict[str, Any]] = Field(default_factory=dict)
    sentiment: Optional[str] = None
    creator_handle: Optional[str] = None
    video_description: Optional[str] = None
    extraction_model: Optional[str] = "gpt-4o-mini"


class AddLocationRequest(BaseModel):
    google_place_id: str = Field(..., description="Google Place ID for the location")
    source: Optional[str] = Field(
        "in-app",
        description="Source of the location add: 'in-app', 'tiktok', 'instagram', or 'magic-search-open'",
    )
    classify_photo: Optional[bool] = Field(True, description="Whether to classify the location's photo with AI")
    generate_emoji: Optional[bool] = Field(True, description="Whether to generate an emoji for the location")
    process_synchronously: Optional[bool] = Field(
        False,
        description="When true, run the full enrichment pipeline inline before returning",
    )
    video_insights: Optional[VideoInsightsPayload] = Field(
        None,
        description="Video insight data to upsert before triggering TikTok vibe blend",
    )


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
    enable_live_web_agent: Optional[bool] = Field(
        False,
        description="When true, return cached AI enrichment and enqueue async refreshes.",
    )

class MagicSearchSection(BaseModel):
    """A lightweight header for grouping the response.

    Recommendations are NOT relisted here — each carries its own ``section``
    field naming the header it belongs to. This list only conveys the ordered
    headers (and their subtitles) the client should render.
    """
    title: str
    subtitle: Optional[str] = None


class MagicSearchResponse(BaseModel):
    user_id: str
    center_lat: float
    center_lon: float
    prompt: str
    radius_km: float
    total_candidates: int
    total_ranked: int
    recommendations: List[MagicLocationRecommendation]
    sections: List[MagicSearchSection] = Field(default_factory=list)
    ai_enrichment: Optional[MagicAIEnrichmentPayload] = None
    debug: Optional[MagicSearchDebug] = None
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


class HiddenGemLocation(BaseModel):
    location_id: int
    name: str
    vicinity: Optional[str] = None
    cuisine_primary: Optional[str] = None
    rating: Optional[float] = None
    user_ratings_total: Optional[int] = None
    price_level: Optional[float] = None
    distance_km: float
    quality_score: float
    hidden_gem_score: float
    rank: int

    # Photo metadata surfaced from get_locations_with_quality (booleans only;
    # resource-name array is hydrated by a separate endpoint on demand).
    image_stored: Optional[bool] = None
    image_unavailable: Optional[bool] = None
    extra_photos_stored: Optional[int] = None


class HiddenGemsResponse(BaseModel):
    center_lat: float
    center_lon: float
    radius_km: float
    total_results: int
    min_reviews: int
    recommendations: List[HiddenGemLocation]
    timestamp: str


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

    # Photo metadata surfaced from get_locations_with_quality (booleans only;
    # resource-name array is hydrated by a separate endpoint on demand).
    image_stored: Optional[bool] = None
    image_unavailable: Optional[bool] = None
    extra_photos_stored: Optional[int] = None

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
