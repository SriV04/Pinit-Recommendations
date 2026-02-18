from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional


@dataclass
class PipelinePaths:
    """Input/output paths required by the recommendation pipeline."""

    data_dir: Path = Path("data/raw")
    city_name: str = "london"
    output_dir: Path = Path("output/pinit")
    user_actions_csv: Optional[Path] = None
    user_friends_csv: Optional[Path] = None
    bubble_locations_csv: Optional[Path] = None

    def details_csv(self) -> Path:
        return self.data_dir / f"{self.city_name}_restaurant_details.csv"

    def reviews_csv(self) -> Path:
        return self.data_dir / f"{self.city_name}_restaurant_reviews.csv"

    def base_csv(self) -> Path:
        return self.data_dir / f"{self.city_name}_restaurants.csv"


DEFAULT_ACTION_WEIGHTS: Dict[str, float] = {
    "save": 3.0,
    "like": 2.0,
    "share_to_bubble": 2.5,
    "detail_view": 0.5,
    "impression": 0.1,
    "dismiss": -1.5,
}

RECENCY_HALFLIFE_DAYS = 30.0


@dataclass
class ReviewTagConfig:
    """Parameters used when deriving tags from free-text reviews."""

    min_unique_authors: int = 2
    min_mentions: int = 3
    english_only: bool = True
    score_floor: float = 20.0
    score_cap: float = 100.0


@dataclass
class RecommendationWeights:
    """Base weights used when blending the different scoring components."""

    taste: float = 0.5
    trend_app: float = 0.15
    hidden_gems: float = 0.2
    quality: float = 0.15
    friend: float = 0.0
    bubble: float = 0.0


@dataclass
class PipelineConfig:
    paths: PipelinePaths = field(default_factory=PipelinePaths)
    review_tagging: ReviewTagConfig = field(default_factory=ReviewTagConfig)
    recommendation_weights: RecommendationWeights = field(
        default_factory=RecommendationWeights
    )
    synthetic_users: bool = True
    top_k_per_user: int = 30


@dataclass
class CacheConfig:
    """Redis cache configuration for proximal recommendations."""

    # Redis connection
    redis_host: str = field(default_factory=lambda: os.getenv("REDIS_HOST", "localhost"))
    redis_port: int = field(default_factory=lambda: int(os.getenv("REDIS_PORT", "6379")))
    redis_db: int = field(default_factory=lambda: int(os.getenv("REDIS_DB", "0")))
    redis_password: Optional[str] = field(default_factory=lambda: os.getenv("REDIS_PASSWORD"))
    redis_ssl: bool = field(default_factory=lambda: os.getenv("REDIS_SSL", "false").lower() == "true")

    # Cache parameters
    large_radius_km: float = field(default_factory=lambda: float(os.getenv("CACHE_LARGE_RADIUS_KM", "15.0")))
    coordinate_precision: int = 2  # Round coords to 0.01 degrees (~1.1km grid)

    # TTLs (seconds)
    unfiltered_cache_ttl: int = field(default_factory=lambda: int(os.getenv("CACHE_UNFILTERED_TTL", "1800")))  # 30 minutes
    taste_score_cache_ttl: int = 3600  # 1 hour

    # Memory limits
    max_cache_entries: int = 10000  # Prevent unbounded growth
    compression_enabled: bool = True  # Gzip compress large values

    # Feature flags
    caching_enabled: bool = field(default_factory=lambda: os.getenv("CACHING_ENABLED", "true").lower() == "true")
