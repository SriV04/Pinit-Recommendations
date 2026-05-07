from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class MagicIntent(BaseModel):
    raw_prompt: str
    cleaned_query: str

    cuisines: List[str] = Field(default_factory=list)
    vibe_tags: List[str] = Field(default_factory=list)
    occasion: Optional[str] = None
    budget: Optional[str] = None
    dietary: List[str] = Field(default_factory=list)
    time_context: Optional[str] = None
    location_name: Optional[str] = None
    location_rectangle: Optional[Dict[str, Dict[str, float]]] = None

    positive_terms: List[str] = Field(default_factory=list)
    negative_terms: List[str] = Field(default_factory=list)

    strict_constraints: Dict[str, Any] = Field(default_factory=dict)
    soft_preferences: Dict[str, Any] = Field(default_factory=dict)

    google_queries: List[str] = Field(default_factory=list)
    included_types: List[str] = Field(default_factory=list)


class MagicSearchDebug(BaseModel):
    source: str = "live"
    magic_search_latency_ms: float = 0.0
    intent_parse_latency_ms: float = 0.0
    google_search_latency_ms: float = 0.0
    place_hydration_latency_ms: float = 0.0
    rerank_latency_ms: float = 0.0
    cache_hit_google_search: bool = False
    cache_hit_place_details: bool = False
    cache_hit_user_profile: bool = False
    total_google_calls: int = 0
    total_candidates_before_dedupe: int = 0
    total_candidates_after_dedupe: int = 0
    total_ranked: int = 0
    google_queries: List[str] = Field(default_factory=list)


class MagicSearchMetrics(MagicSearchDebug):
    pass


class MagicCandidate(BaseModel):
    google_place_id: str
    location_id: Optional[int] = None
    source: List[str] = Field(default_factory=list)

    name: str
    lat: float
    lng: float
    types: List[str] = Field(default_factory=list)
    rating: Optional[float] = None
    user_ratings_total: Optional[int] = None
    price_level: Optional[int] = None
    open_now: Optional[bool] = None

    existing_pinit_data: Optional[Dict[str, Any]] = None
    google_data: Dict[str, Any] = Field(default_factory=dict)
