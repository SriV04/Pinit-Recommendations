#!/bin/bash
set -e

# Configuration
PROJECT_ID="pinit-494520"
IMAGE_NAME="pinit-recommendations"
REGION="europe-west2"
SERVICE_NAME="${SERVICE_NAME:-pinit-recommendations-api}"
AR_REPO="cloud-run-source-deploy"  # Artifact Registry repository

# Rollout knobs (override via env)
#   IMAGE_TAG            image tag to build/push (default: current git short SHA)
#   NO_TRAFFIC=true      deploy a new revision with 0% traffic, reachable at a tag URL
#                        (https://${TRAFFIC_TAG}---<service-url-host>) for testing; shift
#                        traffic afterwards with `gcloud run services update-traffic`.
#   TRAFFIC_TAG          tag for the no-traffic revision (default: canary)
#   SKIP_BUILD=true      reuse the image already pushed for IMAGE_TAG (no docker build/push)
#   PRESERVE_LIVE_CONFIG (default true) keep the service's existing env vars and secrets and only
#                        add the R2/photo settings. The local .env is NOT the source of truth for
#                        a shared live service (it differs: caching, Pub/Sub, dual-write), so the
#                        legacy "replace everything from .env" mode needs PRESERVE_LIVE_CONFIG=false.
#   DEPLOY_PHOTO_DUAL_WRITE  value for PHOTO_DUAL_WRITE_SUPABASE (default true: the previous app
#                        version still reads photos from Supabase Storage)
#   CONFIRM_FULL_ROLLOUT=yes
#                        required to deploy the live service straight to 100% traffic
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo manual)}"
TRAFFIC_TAG="${TRAFFIC_TAG:-canary}"

if [ "${NO_TRAFFIC:-false}" != "true" ] && [ "${SERVICE_NAME}" = "pinit-recommendations-api" ] \
   && [ "${CONFIRM_FULL_ROLLOUT:-}" != "yes" ]; then
  echo "❌ Refusing to deploy the live service '${SERVICE_NAME}' straight to 100% traffic."
  echo "   Canary first:  NO_TRAFFIC=true ./deploy.sh"
  echo "   Or, knowingly: CONFIRM_FULL_ROLLOUT=yes ./deploy.sh"
  exit 1
fi

# Cloud Run sizing knobs (override via env)
API_CPU="${API_CPU:-2}"
API_MEMORY="${API_MEMORY:-2Gi}"
API_MAX_INSTANCES="${API_MAX_INSTANCES:-10}"

# Redis Configuration Options:
# 1. Google Cloud Memorystore (Recommended for production)
#    - Create instance: gcloud redis instances create pinit-redis --size=1 --region=europe-west2
#    - Get host: gcloud redis instances describe pinit-redis --region=europe-west2 --format="get(host)"
#    - Requires VPC connector for Cloud Run to access private IP
#
# 2. External Redis (e.g., Redis Cloud, Upstash)
#    - Set REDIS_HOST to public endpoint
#    - Set REDIS_PASSWORD for authentication
#
# 3. Disable caching (for testing)
#    - Set CACHING_ENABLED=false

# Ensure gcloud is pointed at the right project
gcloud config set project $PROJECT_ID

# Create Artifact Registry repo if it doesn't exist
gcloud artifacts repositories describe $AR_REPO --location=$REGION 2>/dev/null || \
  gcloud artifacts repositories create $AR_REPO --repository-format=docker --location=$REGION

# Configure Docker auth for Artifact Registry
gcloud auth configure-docker ${REGION}-docker.pkg.dev --quiet

AR_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${IMAGE_NAME}:${IMAGE_TAG}"

if [ "${SKIP_BUILD:-false}" = "true" ]; then
  echo "⏭️  SKIP_BUILD=true: reusing ${AR_IMAGE}"
else
  echo "🔨 Building Docker image for linux/amd64..."
  docker build --platform linux/amd64 -t $AR_IMAGE .

  echo "📤 Pushing to Artifact Registry..."
  docker push $AR_IMAGE
fi

echo "🚀 Deploying to Cloud Run..."

# Load environment variables from .env file
if [ -f .env ]; then
  source .env
else
  echo "❌ Error: .env file not found"
  exit 1
fi

if [ -z "$SUPABASE_URL" ]; then
  echo "❌ Error: SUPABASE_URL is not set in .env"
  exit 1
fi

build_env_vars_from_dotenv() {
  local out=""
  while IFS= read -r line || [ -n "$line" ]; do
    # Skip comments/empty lines
    case "$line" in
      ""|\#*) continue ;;
    esac

    # Support "export KEY=VALUE"
    line="${line#export }"

    if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)= ]]; then
      local key="${BASH_REMATCH[1]}"
      case "$key" in
        # Bound via --set-secrets below (avoid duplicates)
        SUPABASE_SERVICE_KEY|SUPABASE_SERVICE_ROLE_KEY|SUPABASE_SECRET_KEY|GOOGLE_PLACE_API_KEY|REDIS_PASSWORD|R2_ACCESS_KEY_ID|R2_SECRET_ACCESS_KEY)
          continue
          ;;
        # These are set explicitly per deploy (avoid duplicates)
        GOOGLE_CLOUD_PROJECT|PUBSUB_ENABLED|PUBSUB_TOPIC_LOCATION_TASKS|PUBSUB_PROJECT_ID)
          continue
          ;;
      esac

      local value="${!key}"
      # Escape backslashes and commas for gcloud --set-env-vars
      value="${value//\\/\\\\}"
      value="${value//,/\\,}"

      out+="${out:+,}${key}=${value}"
    fi
  done < .env
  echo "$out"
}

# Pass all .env vars (except those set via --set-secrets and a few computed keys).
ENV_VARS="$(build_env_vars_from_dotenv)"

# Defaults for production Cloud Run deploys (override by exporting before running deploy.sh).
: "${PUBSUB_ENABLED:=true}"
: "${PUBSUB_TOPIC_LOCATION_TASKS:=location-tasks}"
: "${WARM_CACHE_ENABLED:=true}"
: "${WARM_CACHE_INTERVAL_SECONDS:=900}"
: "${WARM_CACHE_ZONE_SET:=london}"
: "${WARM_CACHE_START_HOUR:=9}"
: "${WARM_CACHE_END_HOUR:=21}"
: "${WARM_CACHE_TIMEZONE:=Europe/London}"
: "${CACHE_UNFILTERED_TTL:=3600}"

# Keep the API on its in-process fallback until pubsub.sh has deployed and
# verified workers, subscriptions, retry policy, dead-lettering, and IAM.
if [ "${DEPLOY_PUBSUB:-false}" = "true" ]; then
  PUBSUB_ENABLED="false"
fi

# Ensure Pub/Sub + project vars are always set explicitly (even if not in .env)
ENV_VARS+="${ENV_VARS:+,}GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
ENV_VARS+=",PUBSUB_ENABLED=${PUBSUB_ENABLED}"
ENV_VARS+=",PUBSUB_TOPIC_LOCATION_TASKS=${PUBSUB_TOPIC_LOCATION_TASKS}"
ENV_VARS+=",WARM_CACHE_ENABLED=${WARM_CACHE_ENABLED}"
ENV_VARS+=",WARM_CACHE_INTERVAL_SECONDS=${WARM_CACHE_INTERVAL_SECONDS}"
ENV_VARS+=",WARM_CACHE_ZONE_SET=${WARM_CACHE_ZONE_SET}"
ENV_VARS+=",WARM_CACHE_START_HOUR=${WARM_CACHE_START_HOUR}"
ENV_VARS+=",WARM_CACHE_END_HOUR=${WARM_CACHE_END_HOUR}"
ENV_VARS+=",WARM_CACHE_TIMEZONE=${WARM_CACHE_TIMEZONE}"
ENV_VARS+=",CACHE_UNFILTERED_TTL=${CACHE_UNFILTERED_TTL}"
if [ -n "$PUBSUB_PROJECT_ID" ]; then
  ENV_VARS+=",PUBSUB_PROJECT_ID=${PUBSUB_PROJECT_ID}"
fi

# Secret Manager bindings: ENV_VAR=secret-name:version
# Requires: secrets created in Secret Manager AND the Cloud Run service
# account granted roles/secretmanager.secretAccessor on each.
has_enabled_secret_version() {
  local secret_name="$1"
  gcloud secrets versions list "${secret_name}" \
    --project "${PROJECT_ID}" \
    --filter='state:ENABLED' \
    --format='value(name)' \
    --limit=1 >/dev/null 2>&1
}

if gcloud secrets describe "supabase-service-key" --project "${PROJECT_ID}" >/dev/null 2>&1 && has_enabled_secret_version "supabase-service-key"; then
  SUPABASE_SERVICE_SECRET_NAME="supabase-service-key"
elif gcloud secrets describe "supabase-service-role-key" --project "${PROJECT_ID}" >/dev/null 2>&1 && has_enabled_secret_version "supabase-service-role-key"; then
  SUPABASE_SERVICE_SECRET_NAME="supabase-service-role-key"
elif gcloud secrets describe "supabase-service-key" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "❌ Error: Secret 'supabase-service-key' exists but has no ENABLED versions."
  echo "   Add a version, e.g.:"
  echo "     printf \"<SUPABASE_SERVICE_KEY>\" | gcloud secrets versions add supabase-service-key --data-file=- --project \"${PROJECT_ID}\""
  exit 1
elif gcloud secrets describe "supabase-service-role-key" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "❌ Error: Secret 'supabase-service-role-key' exists but has no ENABLED versions."
  echo "   Add a version, e.g.:"
  echo "     printf \"<SUPABASE_SERVICE_ROLE_KEY>\" | gcloud secrets versions add supabase-service-role-key --data-file=- --project \"${PROJECT_ID}\""
  exit 1
else
  echo "❌ Error: Could not find a Supabase service key secret with an ENABLED version in project '${PROJECT_ID}'."
  echo "   Expected either 'supabase-service-key' (preferred) or 'supabase-service-role-key' (legacy)."
  exit 1
fi

SECRETS="SUPABASE_SERVICE_KEY=${SUPABASE_SERVICE_SECRET_NAME}:latest"
SECRETS+=",GOOGLE_PLACE_API_KEY=google-place-api-key:latest"
SECRETS+=",REDIS_PASSWORD=redis-password:latest"

# Cloudflare R2 photo storage (optional). When R2_SECRET_ACCESS_KEY is set in .env the
# two credentials must live in Secret Manager; they are never passed as plain env vars.
# R2_ACCOUNT_ID, R2_BUCKET_NAME and PHOTO_CDN_BASE_URL are not secret and pass through .env.
if [ -n "${R2_SECRET_ACCESS_KEY:-}" ]; then
  for r2_pair in "R2_ACCESS_KEY_ID:r2-access-key-id" "R2_SECRET_ACCESS_KEY:r2-secret-access-key"; do
    r2_env_name="${r2_pair%%:*}"
    r2_secret_name="${r2_pair##*:}"
    if gcloud secrets describe "${r2_secret_name}" --project "${PROJECT_ID}" >/dev/null 2>&1 && has_enabled_secret_version "${r2_secret_name}"; then
      SECRETS+=",${r2_env_name}=${r2_secret_name}:latest"
    else
      echo "❌ Error: R2 is configured in .env but secret '${r2_secret_name}' has no ENABLED version in project '${PROJECT_ID}'."
      echo "   Create it (value is read from stdin, not shown), e.g.:"
      echo "     gcloud secrets create ${r2_secret_name} --project \"${PROJECT_ID}\" --replication-policy=automatic 2>/dev/null || true"
      echo "     printf \"<${r2_env_name}>\" | gcloud secrets versions add ${r2_secret_name} --data-file=- --project \"${PROJECT_ID}\""
      echo "   Then grant the Cloud Run service account roles/secretmanager.secretAccessor on it."
      exit 1
    fi
  done
fi

PRESERVE_LIVE_CONFIG="${PRESERVE_LIVE_CONFIG:-true}"
if [ "${PRESERVE_LIVE_CONFIG}" = "true" ]; then
  for required in R2_ACCOUNT_ID R2_BUCKET_NAME PHOTO_CDN_BASE_URL; do
    if [ -z "${!required:-}" ]; then
      echo "❌ ${required} is not set in .env (needed for the R2 photo settings)."
      exit 1
    fi
  done
  # Everything already on the service (Redis, caching, Pub/Sub, warm cache, secrets) is kept.
  CONFIG_ARGS=(
    --update-env-vars "R2_ACCOUNT_ID=${R2_ACCOUNT_ID},R2_BUCKET_NAME=${R2_BUCKET_NAME},PHOTO_CDN_BASE_URL=${PHOTO_CDN_BASE_URL},PHOTO_DUAL_WRITE_SUPABASE=${DEPLOY_PHOTO_DUAL_WRITE:-true}"
    --update-secrets "R2_ACCESS_KEY_ID=r2-access-key-id:latest,R2_SECRET_ACCESS_KEY=r2-secret-access-key:latest"
  )
  echo "🔒 PRESERVE_LIVE_CONFIG=true: keeping the service's existing env vars and secrets; adding R2/photo settings only."
else
  CONFIG_ARGS=(--set-env-vars "$ENV_VARS" --set-secrets "$SECRETS")
fi

TRAFFIC_ARGS=()
if [ "${NO_TRAFFIC:-false}" = "true" ]; then
  TRAFFIC_ARGS=(--no-traffic --tag "${TRAFFIC_TAG}")
  echo "🐤 Canary deploy: new revision gets 0% traffic, tagged '${TRAFFIC_TAG}'."
fi

gcloud run deploy $SERVICE_NAME \
  --image $AR_IMAGE \
  "${TRAFFIC_ARGS[@]}" \
  --platform managed \
  --region $REGION \
  --allow-unauthenticated \
  "${CONFIG_ARGS[@]}" \
  --cpu-throttling \
  --memory "${API_MEMORY}" \
  --timeout 540 \
  --max-instances "${API_MAX_INSTANCES}" \
  --min-instances 0 \
  --cpu "${API_CPU}"

echo "✅ Deployment complete!"
echo "📍 Service URL:"
gcloud run services describe $SERVICE_NAME --region $REGION --format 'value(status.url)'
echo ""
if [ "${PRESERVE_LIVE_CONFIG}" = "true" ]; then
  echo "ℹ️  Service config preserved from the live revision (not read from .env)."
fi
echo "ℹ️  Redis Caching (from local .env, informational only when config is preserved): ${CACHING_ENABLED:-true}"
if [ -n "$REDIS_HOST" ]; then
  echo "   Redis Host: ${REDIS_HOST}"
else
  echo "   ⚠️  No REDIS_HOST set - caching will be disabled"
fi

if [ "${DEPLOY_PUBSUB:-false}" = "true" ]; then
  echo ""
  echo "🔧 DEPLOY_PUBSUB=true: deploying Pub/Sub workers + subscription push config..."
  export PUBSUB_ENABLED="true"
  export PUBSUB_TOPIC_LOCATION_TASKS="location-tasks"
  ./pubsub.sh
fi
