from __future__ import annotations

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
