#!/bin/bash
set -e

# Configuration
PROJECT_ID="project-add4b0f5-0080-47ef-80f"
IMAGE_NAME="pinit-recommendations"
REGION="europe-west2"
SERVICE_NAME="pinit-recommendations-api"
AR_REPO="cloud-run-source-deploy"  # Artifact Registry repository

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

AR_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${IMAGE_NAME}:latest"

echo "🔨 Building Docker image for linux/amd64..."
docker build --platform linux/amd64 -t $AR_IMAGE .

echo "📤 Pushing to Artifact Registry..."
docker push $AR_IMAGE

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
        SUPABASE_SERVICE_ROLE_KEY|GOOGLE_PLACE_API_KEY|REDIS_PASSWORD)
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

# Ensure Pub/Sub + project vars are always set explicitly (even if not in .env)
ENV_VARS+="${ENV_VARS:+,}GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
if [ -n "$PUBSUB_ENABLED" ]; then
  ENV_VARS+=",PUBSUB_ENABLED=${PUBSUB_ENABLED}"
fi
if [ -n "$PUBSUB_TOPIC_LOCATION_TASKS" ]; then
  ENV_VARS+=",PUBSUB_TOPIC_LOCATION_TASKS=${PUBSUB_TOPIC_LOCATION_TASKS}"
fi
if [ -n "$PUBSUB_PROJECT_ID" ]; then
  ENV_VARS+=",PUBSUB_PROJECT_ID=${PUBSUB_PROJECT_ID}"
fi

# Secret Manager bindings: ENV_VAR=secret-name:version
# Requires: secrets created in Secret Manager AND the Cloud Run service
# account granted roles/secretmanager.secretAccessor on each.
SECRETS="SUPABASE_SERVICE_ROLE_KEY=supabase-service-role-key:latest"
SECRETS+=",GOOGLE_PLACE_API_KEY=google-place-api-key:latest"
SECRETS+=",REDIS_PASSWORD=redis-password:latest"

gcloud run deploy $SERVICE_NAME \
  --image $AR_IMAGE \
  --platform managed \
  --region $REGION \
  --allow-unauthenticated \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "$SECRETS" \
  --memory 2Gi \
  --timeout 540 \
  --max-instances 10 \
  --min-instances 0 \
  --cpu 2

echo "✅ Deployment complete!"
echo "📍 Service URL:"
gcloud run services describe $SERVICE_NAME --region $REGION --format 'value(status.url)'
echo ""
echo "ℹ️  Redis Caching: ${CACHING_ENABLED:-true}"
if [ -n "$REDIS_HOST" ]; then
  echo "   Redis Host: ${REDIS_HOST}"
else
  echo "   ⚠️  No REDIS_HOST set - caching will be disabled"
fi
