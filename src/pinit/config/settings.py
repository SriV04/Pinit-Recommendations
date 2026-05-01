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
    """Base weights used when blending the different scoring components.

    The blend is split into two pillars:

    APP signal (~0.80)              CATALOG signal (~0.20)
      app_engagement   0.30           google_baseline  0.05
      social           0.18           vibe             0.07
      collaborative    0.12           dietary          0.08
      video_insight    0.10
      ────────                        ────────
      0.70                            0.20

    Plus an extra 0.10 buffer absorbed by share_boost (a *post*-blend
    multiplier, not a weight in this sum). Weights are normalised to 1.0 at
    use-time, so it's safe to bump any one without rebalancing the rest.

    Keep these conservative — `share_boost` and `dietary_penalty` are the
    teeth of the new system. The weights only allocate the smooth signal.
    """

    # APP-signal pillars
    app_engagement: float = 0.30   # location_popularity_app saves/dislikes/been_to
    social: float = 0.18           # friend graph
    collaborative: float = 0.12    # item-item co-save similarity
    video_insight: float = 0.10    # video_insights count + sentiment + creators

    # CATALOG-signal pillars
    google_baseline: float = 0.05  # Bayesian rating × log(reviews)
    vibe: float = 0.07             # vibe_vector cosine
    dietary: float = 0.08          # dietary_vector match (penalty applied separately)

    # ── Backwards-compat fields ─────────────────────────────────────────
    # Legacy callers passed a single `quality` weight. When supplied it's
    # split 5:1 into app_engagement + google_baseline in __post_init__.
    # After init `quality` always equals the SUM so reads keep working.
    quality: Optional[float] = None
    # Older taste/trend pipeline keys that some callers still pass via
    # asdict() — accepted and ignored, kept on the instance so any code
    # still reading them sees a 0.0 default.
    trend_app: float = 0.0
    hidden_gems: float = 0.0
    taste: float = 0.0

    def __post_init__(self) -> None:
        if self.quality is not None:
            self.app_engagement = self.quality * (5.0 / 6.0)
            self.google_baseline = self.quality * (1.0 / 6.0)
        self.quality = self.app_engagement + self.google_baseline


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
    # Grid precision in decimal places — how aggressively we snap request
    # coordinates before building the cache key. Coarser grids share cache
    # entries across more users → higher hit rate.
    #
    #   precision=2  →  0.01° (~1.1 km)  many entries, low hit rate
    #   precision=1  →  0.1°  (~11 km)   few entries, high hit rate  ← default
    #
    # Hit check needs `distance_to_cached_centre + request_radius <= cached_radius`.
    # At precision=1 the worst-case offset from snapped centre is ~6.5 km
    # (cell corner at London latitude). With cached_radius=15 km that means
    # requests up to ~8.5 km radius still hit. The app's default request
    # radius is 2 km and typical max is 5 km, so precision=1 is safe.
    coordinate_precision: int = field(default_factory=lambda: int(os.getenv("CACHE_COORDINATE_PRECISION", "1")))

    # TTLs (seconds)
    unfiltered_cache_ttl: int = field(default_factory=lambda: int(os.getenv("CACHE_UNFILTERED_TTL", "3600")))  # 60 minutes
    taste_score_cache_ttl: int = 3600  # 1 hour

    # Memory limits
    max_cache_entries: int = 10000  # Prevent unbounded growth
    compression_enabled: bool = True  # Gzip compress large values

    # Feature flags
    caching_enabled: bool = field(default_factory=lambda: os.getenv("CACHING_ENABLED", "true").lower() == "true")
