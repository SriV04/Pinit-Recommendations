from __future__ import annotations

import logging
import os

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
        logger.debug("Loaded %s from environment variable", secret_id)
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
                logger.debug("Loaded %s from Secret Manager", secret_id)
                return secret_value
        except Exception as exc:
            logger.warning("Could not fetch secret '%s' from Secret Manager: %s", secret_id, exc)

    # Priority 3: Return empty string (will fail downstream if truly required)
    logger.warning("Secret '%s' not found in environment or Secret Manager", secret_id)
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
