#!/bin/bash
set -e

PROJECT="${PROJECT:-pinit-494520}"
REGION="${REGION:-europe-west2}"
API_SERVICE="${API_SERVICE:-pinit-recommendations-api}"
WORKER_FAST_SERVICE="${WORKER_FAST_SERVICE:-pinit-location-worker}"
WORKER_MENU_SERVICE="${WORKER_MENU_SERVICE:-pinit-location-worker-menu}"
TOPIC="${PUBSUB_TOPIC_LOCATION_TASKS:-location-tasks}"

# Cloud Run sizing knobs (defaults chosen to fit common regional quotas; override via env)
FAST_CPU="${FAST_CPU:-1}"
FAST_MEMORY="${FAST_MEMORY:-1Gi}"
FAST_CONCURRENCY="${FAST_CONCURRENCY:-1}"
FAST_MAX_INSTANCES="${FAST_MAX_INSTANCES:-5}"

MENU_CPU="${MENU_CPU:-2}"
MENU_MEMORY="${MENU_MEMORY:-4Gi}"
MENU_CONCURRENCY="${MENU_CONCURRENCY:-1}"
MENU_MAX_INSTANCES="${MENU_MAX_INSTANCES:-2}"

# Load environment variables from .env file (required for SUPABASE_URL and optional keys).
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

# Ensure Pub/Sub resources exist before enabling Pub/Sub on services.
gcloud services enable pubsub.googleapis.com --project "${PROJECT}" >/dev/null 2>&1 || true

if ! gcloud pubsub topics describe "${TOPIC}" --project "${PROJECT}" >/dev/null 2>&1; then
  echo "ℹ️  Creating Pub/Sub topic: ${TOPIC}"
  gcloud pubsub topics create "${TOPIC}" --project "${PROJECT}"
fi

# Ensure subscriptions exist (one per task_type with attribute filter).
for TASK in pipeline details_enrich emoji photos menu_vibe vibe_reprocess; do
  SUB="${TOPIC}-${TASK}"
  if ! gcloud pubsub subscriptions describe "${SUB}" --project "${PROJECT}" >/dev/null 2>&1; then
    echo "ℹ️  Creating Pub/Sub subscription: ${SUB}"
    create_args=(
      gcloud pubsub subscriptions create "${SUB}"
      --project "${PROJECT}"
      --topic "${TOPIC}"
      --message-filter="attributes.task_type=\"${TASK}\""
      --enable-message-ordering
    )
    if [ "${TASK}" = "menu_vibe" ]; then
      create_args+=(--ack-deadline 600)
    fi
    "${create_args[@]}"
  fi
done

build_env_vars_from_dotenv() {
  local out=""
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ""|\#*) continue ;;
    esac
    line="${line#export }"

    if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)= ]]; then
      local key="${BASH_REMATCH[1]}"
      case "$key" in
        SUPABASE_SERVICE_KEY|SUPABASE_SERVICE_ROLE_KEY|SUPABASE_SECRET_KEY|GOOGLE_PLACE_API_KEY|REDIS_PASSWORD|XAI_API_KEY)
          continue
          ;;
        GOOGLE_CLOUD_PROJECT|PUBSUB_ENABLED|PUBSUB_TOPIC_LOCATION_TASKS|PUBSUB_PROJECT_ID)
          continue
          ;;
      esac

      local value="${!key}"
      value="${value//\\/\\\\}"
      value="${value//,/\\,}"
      out+="${out:+,}${key}=${value}"
    fi
  done < .env
  echo "$out"
}

: "${WARM_CACHE_ENABLED:=true}"
: "${WARM_CACHE_INTERVAL_SECONDS:=900}"
: "${WARM_CACHE_ZONE_SET:=london}"
: "${WARM_CACHE_START_HOUR:=9}"
: "${WARM_CACHE_END_HOUR:=21}"
: "${WARM_CACHE_TIMEZONE:=Europe/London}"
: "${CACHE_UNFILTERED_TTL:=3600}"

ENV_VARS="$(build_env_vars_from_dotenv)"
ENV_VARS+="${ENV_VARS:+,}GOOGLE_CLOUD_PROJECT=${PROJECT},PUBSUB_ENABLED=true,PUBSUB_TOPIC_LOCATION_TASKS=${TOPIC}"
ENV_VARS+=",WARM_CACHE_ENABLED=${WARM_CACHE_ENABLED}"
ENV_VARS+=",WARM_CACHE_INTERVAL_SECONDS=${WARM_CACHE_INTERVAL_SECONDS}"
ENV_VARS+=",WARM_CACHE_ZONE_SET=${WARM_CACHE_ZONE_SET}"
ENV_VARS+=",WARM_CACHE_START_HOUR=${WARM_CACHE_START_HOUR}"
ENV_VARS+=",WARM_CACHE_END_HOUR=${WARM_CACHE_END_HOUR}"
ENV_VARS+=",WARM_CACHE_TIMEZONE=${WARM_CACHE_TIMEZONE}"
ENV_VARS+=",CACHE_UNFILTERED_TTL=${CACHE_UNFILTERED_TTL}"

gcloud run services update "${API_SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --set-env-vars "${ENV_VARS}"

API_SA="$(gcloud run services describe "${API_SERVICE}" --project "${PROJECT}" --region "${REGION}" --format='value(spec.template.spec.serviceAccountName)')"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member "serviceAccount:${API_SA}" \
  --role "roles/pubsub.publisher"

IMAGE="europe-west2-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/pinit-recommendations:latest"
RUN_SA="${API_SA}"

WORKER_ENV_VARS="${ENV_VARS}"

# Secret Manager bindings: ENV_VAR=secret-name:version
has_enabled_secret_version() {
  local secret_name="$1"
  gcloud secrets versions list "${secret_name}" \
    --project "${PROJECT}" \
    --filter='state:ENABLED' \
    --format='value(name)' \
    --limit=1 >/dev/null 2>&1
}

if gcloud secrets describe "supabase-service-key" --project "${PROJECT}" >/dev/null 2>&1 && has_enabled_secret_version "supabase-service-key"; then
  SUPABASE_SERVICE_SECRET_NAME="supabase-service-key"
elif gcloud secrets describe "supabase-service-role-key" --project "${PROJECT}" >/dev/null 2>&1 && has_enabled_secret_version "supabase-service-role-key"; then
  SUPABASE_SERVICE_SECRET_NAME="supabase-service-role-key"
elif gcloud secrets describe "supabase-service-key" --project "${PROJECT}" >/dev/null 2>&1; then
  echo "❌ Error: Secret 'supabase-service-key' exists but has no ENABLED versions."
  echo "   Add a version, e.g.:"
  echo "     printf \"<SUPABASE_SERVICE_KEY>\" | gcloud secrets versions add supabase-service-key --data-file=- --project \"${PROJECT}\""
  exit 1
elif gcloud secrets describe "supabase-service-role-key" --project "${PROJECT}" >/dev/null 2>&1; then
  echo "❌ Error: Secret 'supabase-service-role-key' exists but has no ENABLED versions."
  echo "   Add a version, e.g.:"
  echo "     printf \"<SUPABASE_SERVICE_ROLE_KEY>\" | gcloud secrets versions add supabase-service-role-key --data-file=- --project \"${PROJECT}\""
  exit 1
else
  echo "❌ Error: Could not find a Supabase service key secret with an ENABLED version in project '${PROJECT}'."
  echo "   Expected either 'supabase-service-key' (preferred) or 'supabase-service-role-key' (legacy)."
  exit 1
fi

FAST_SECRETS="SUPABASE_SERVICE_KEY=${SUPABASE_SERVICE_SECRET_NAME}:latest"
FAST_SECRETS+=",GOOGLE_PLACE_API_KEY=google-place-api-key:latest"
FAST_SECRETS+=",REDIS_PASSWORD=redis-password:latest"

MENU_SECRETS="${FAST_SECRETS}"
MENU_ENV_VARS="${WORKER_ENV_VARS}"

if gcloud secrets describe "xai-api-key" --project "${PROJECT}" >/dev/null 2>&1; then
  FAST_SECRETS+=",XAI_API_KEY=xai-api-key:latest"
  MENU_SECRETS+=",XAI_API_KEY=xai-api-key:latest"
else
  # Fallback: if XAI_API_KEY is present in the local environment/.env, pass it as a plain env var.
  # Prefer Secret Manager for production.
  if [ -n "${XAI_API_KEY:-}" ]; then
    escaped="${XAI_API_KEY//\\/\\\\}"
    escaped="${escaped//,/\\,}"
    WORKER_ENV_VARS+=",XAI_API_KEY=${escaped}"
    MENU_ENV_VARS+=",XAI_API_KEY=${escaped}"
  else
    echo "❌ Error: Secret 'xai-api-key' not found and XAI_API_KEY not set."
    echo "   Vibe tagging (menu_vibe/vibe_reprocess) will fail and Pub/Sub will retry forever."
    echo "   Fix by creating the secret in project '${PROJECT}' or setting XAI_API_KEY in .env."
    exit 1
  fi
fi

gcloud run deploy "${WORKER_FAST_SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --image "${IMAGE}" \
  --service-account "${RUN_SA}" \
  --no-allow-unauthenticated \
  --timeout 900 \
  --cpu "${FAST_CPU}" \
  --memory "${FAST_MEMORY}" \
  --concurrency "${FAST_CONCURRENCY}" \
  --max-instances "${FAST_MAX_INSTANCES}" \
  --set-env-vars "${WORKER_ENV_VARS}" \
  --set-secrets "${FAST_SECRETS}" \
  --command uvicorn \
  --args "pinit.worker.main:app,--host,0.0.0.0,--port,8080"

gcloud run deploy "${WORKER_MENU_SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --image "${IMAGE}" \
  --service-account "${RUN_SA}" \
  --no-allow-unauthenticated \
  --timeout 900 \
  --cpu "${MENU_CPU}" \
  --memory "${MENU_MEMORY}" \
  --concurrency "${MENU_CONCURRENCY}" \
  --max-instances "${MENU_MAX_INSTANCES}" \
  --set-env-vars "${MENU_ENV_VARS}" \
  --set-secrets "${MENU_SECRETS}" \
  --command uvicorn \
  --args "pinit.worker.main:app,--host,0.0.0.0,--port,8080"

PUSH_SA="${RUN_SA}"

gcloud run services add-iam-policy-binding "${WORKER_FAST_SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --member "serviceAccount:${PUSH_SA}" \
  --role "roles/run.invoker"

gcloud run services add-iam-policy-binding "${WORKER_MENU_SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --member "serviceAccount:${PUSH_SA}" \
  --role "roles/run.invoker"


FAST_WORKER_URL="$(gcloud run services describe "${WORKER_FAST_SERVICE}" --project "${PROJECT}" --region "${REGION}" --format='value(status.url)')"
MENU_WORKER_URL="$(gcloud run services describe "${WORKER_MENU_SERVICE}" --project "${PROJECT}" --region "${REGION}" --format='value(status.url)')"

for TASK in pipeline details_enrich emoji photos vibe_reprocess; do
  gcloud pubsub subscriptions update "${TOPIC}-${TASK}" \
    --project "${PROJECT}" \
    --push-endpoint "${FAST_WORKER_URL}/internal/pubsub/location-tasks" \
    --push-auth-service-account "${PUSH_SA}"
done

gcloud pubsub subscriptions update "${TOPIC}-menu_vibe" \
  --project "${PROJECT}" \
  --ack-deadline 600 \
  --push-endpoint "${MENU_WORKER_URL}/internal/pubsub/location-tasks" \
  --push-auth-service-account "${PUSH_SA}"

echo "✅ Pub/Sub deployment complete."
echo "   Topic: ${TOPIC}"
echo "   Fast worker: ${FAST_WORKER_URL}"
echo "   Menu worker: ${MENU_WORKER_URL}"
