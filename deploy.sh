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

# Build environment variables string (NON-SENSITIVE values only).
# Sensitive values (service role key, API keys, redis password) come from
# Secret Manager via --set-secrets below.
ENV_VARS="SUPABASE_URL=${SUPABASE_URL}"

# Redis configuration (optional - will use defaults if not set)
if [ -n "$REDIS_HOST" ]; then
  ENV_VARS+=",REDIS_HOST=${REDIS_HOST}"
fi

if [ -n "$REDIS_PORT" ]; then
  ENV_VARS+=",REDIS_PORT=${REDIS_PORT}"
fi

if [ -n "$REDIS_DB" ]; then
  ENV_VARS+=",REDIS_DB=${REDIS_DB}"
fi

if [ -n "$REDIS_SSL" ]; then
  ENV_VARS+=",REDIS_SSL=${REDIS_SSL}"
fi

if [ -n "$CACHING_ENABLED" ]; then
  ENV_VARS+=",CACHING_ENABLED=${CACHING_ENABLED}"
fi

if [ -n "$CACHE_LARGE_RADIUS_KM" ]; then
  ENV_VARS+=",CACHE_LARGE_RADIUS_KM=${CACHE_LARGE_RADIUS_KM}"
fi

if [ -n "$CACHE_UNFILTERED_TTL" ]; then
  ENV_VARS+=",CACHE_UNFILTERED_TTL=${CACHE_UNFILTERED_TTL}"
fi

if [ -n "$CACHE_COORDINATE_PRECISION" ]; then
  ENV_VARS+=",CACHE_COORDINATE_PRECISION=${CACHE_COORDINATE_PRECISION}"
fi

if [ -n "$XAI_API_KEY" ]; then
  ENV_VARS+=",XAI_API_KEY=${XAI_API_KEY}"
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