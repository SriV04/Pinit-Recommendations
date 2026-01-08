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


# ==================== SECRETS MANAGEMENT ====================
# Supports both local development (via .env) and GCP deployment (via Secret Manager)

import os
import logging
from dotenv import load_dotenv

# Load .env file for local development
load_dotenv()

logger = logging.getLogger(__name__)

# Try to import Google Secret Manager (optional, only needed in GCP)
try:
    from google.cloud import secretmanager
    HAS_SECRET_MANAGER = True
except ImportError:
    HAS_SECRET_MANAGER = False


def get_secret(secret_id: str) -> str:
    """
    Fetch a secret from environment or Google Secret Manager.
    
    Priority:
    1. Environment variable (e.g., SUPABASE_URL for secret-id "supabase-url")
    2. Google Secret Manager (if running in GCP)
    3. Empty string (fallback)
    
    Args:
        secret_id: Secret identifier (e.g., "supabase-url")
        
    Returns:
        Secret value as string
    """
    # Convert secret-id to ENV_VAR format
    env_var_name = secret_id.upper().replace("-", "_")
    
    # Priority 1: Check environment variables (from .env or Cloud Run config)
    env_value = os.getenv(env_var_name)
    if env_value:
        logger.debug(f"Loaded {secret_id} from environment variable")
        return env_value
    
    # Priority 2: Try Secret Manager (only works in GCP with proper permissions)
    if HAS_SECRET_MANAGER:
        try:
            project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
            if project_id:
                client = secretmanager.SecretManagerServiceClient()
                name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
                response = client.access_secret_version(request={"name": name})
                secret_value = response.payload.data.decode("UTF-8")
                logger.debug(f"Loaded {secret_id} from Secret Manager")
                return secret_value
        except Exception as e:
            logger.warning(f"Could not fetch secret '{secret_id}' from Secret Manager: {e}")
    
    # Priority 3: Return empty string (will fail downstream if truly required)
    logger.warning(f"Secret '{secret_id}' not found in environment or Secret Manager")
    return ""


# Load all secrets
SUPABASE_URL = get_secret("supabase-url")

# Be permissive with service role key naming:
# - ENV: SUPABASE_SERVICE_ROLE_KEY (common)
# - Secret IDs: supabase-service-role-key or supabase-service-key (legacy)
SUPABASE_SERVICE_ROLE_KEY = (
    get_secret("supabase-service-role-key")
    or get_secret("supabase-service-key")
    or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
)

SUPABASE_ANON_KEY = get_secret("supabase-anon-key")
GOOGLE_MAPS_API_KEY = get_secret("google-maps-api-key")
GEMINI_API_KEY = get_secret("gemini-api-key")
GOOGLE_PLACE_API_KEY = get_secret("google-place-api-key")
