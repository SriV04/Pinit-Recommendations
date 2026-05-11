from __future__ import annotations

import logging
import os
import base64
import json

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
        return env_value.strip()

    # Priority 2: Try Secret Manager (only works in GCP with proper permissions)
    if HAS_SECRET_MANAGER:
        try:
            project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
            if project_id:
                client = secretmanager.SecretManagerServiceClient()
                name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
                response = client.access_secret_version(request={"name": name})
                secret_value = response.payload.data.decode("UTF-8").strip()
                logger.debug("Loaded %s from Secret Manager", secret_id)
                return secret_value
        except Exception as exc:
            logger.warning("Could not fetch secret '%s' from Secret Manager: %s", secret_id, exc)

    # Priority 3: Return empty string (will fail downstream if truly required)
    logger.warning("Secret '%s' not found in environment or Secret Manager", secret_id)
    return ""

def _decode_supabase_key_role(key: str) -> str:
    """Best-effort decode of Supabase API key JWT payload `role` claim."""
    try:
        parts = (key or "").strip().split(".")
        if len(parts) < 2:
            return ""
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8"))
        role = payload.get("role")
        return str(role) if role is not None else ""
    except Exception:
        return ""


# Load all secrets
SUPABASE_URL = get_secret("supabase-url")

# Be permissive with Supabase service key naming:
# - ENV: SUPABASE_SERVICE_KEY (preferred), SUPABASE_SERVICE_ROLE_KEY (common)
# - Secret IDs: supabase-service-key (preferred), supabase-service-role-key (legacy)
# NOTE: Do not use the Supabase "JWT secret" here; it is NOT a valid API key for PostgREST/Storage.
_primary_service_key = get_secret("supabase-service-key") or os.getenv("SUPABASE_SERVICE_KEY", "")
_primary_role = _decode_supabase_key_role(_primary_service_key)

_legacy_service_key = ""
_legacy_role = ""
if _primary_role != "service_role":
    _legacy_service_key = get_secret("supabase-service-role-key") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    _legacy_role = _decode_supabase_key_role(_legacy_service_key)

if _primary_role == "service_role":
    SUPABASE_SERVICE_KEY = _primary_service_key
elif _legacy_role == "service_role":
    SUPABASE_SERVICE_KEY = _legacy_service_key
else:
    SUPABASE_SERVICE_KEY = _primary_service_key or _legacy_service_key
    if SUPABASE_SERVICE_KEY:
        logger.error(
            "Loaded Supabase service key but role=%r (expected 'service_role'). "
            "Set SUPABASE_SERVICE_KEY/SUPABASE_SERVICE_ROLE_KEY to the service_role API key.",
            _decode_supabase_key_role(SUPABASE_SERVICE_KEY) or None,
        )

# Backwards-compatible alias (many modules still reference this name).
SUPABASE_SERVICE_ROLE_KEY = SUPABASE_SERVICE_KEY

SUPABASE_ANON_KEY = get_secret("supabase-anon-key")
GEMINI_API_KEY = get_secret("gemini-api-key")
GOOGLE_PLACE_API_KEY = get_secret("google-place-api-key")
XAI_API_KEY = get_secret("xai-api-key")
