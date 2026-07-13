#!/bin/bash
set -euo pipefail

PROJECT="${PROJECT:-pinit-494520}"
REGION="${REGION:-europe-west2}"
API_SERVICE="${API_SERVICE:-pinit-recommendations-api}"
WORKER_FAST_SERVICE="${WORKER_FAST_SERVICE:-pinit-location-worker}"
WORKER_MENU_SERVICE="${WORKER_MENU_SERVICE:-pinit-location-worker-menu}"
TOPIC="${PUBSUB_TOPIC_LOCATION_TASKS:-location-tasks}"
DEAD_LETTER_TOPIC="${PUBSUB_DEAD_LETTER_TOPIC:-${TOPIC}-dead-letter}"
DEAD_LETTER_SUBSCRIPTION="${PUBSUB_DEAD_LETTER_SUBSCRIPTION:-${DEAD_LETTER_TOPIC}-inspect}"
MIN_RETRY_DELAY="${PUBSUB_MIN_RETRY_DELAY:-10s}"
MAX_RETRY_DELAY="${PUBSUB_MAX_RETRY_DELAY:-600s}"
MAX_DELIVERY_ATTEMPTS="${PUBSUB_MAX_DELIVERY_ATTEMPTS:-10}"
ACK_DEADLINE_SECONDS="${PUBSUB_ACK_DEADLINE_SECONDS:-600}"

FAST_CPU="${FAST_CPU:-1}"
FAST_MEMORY="${FAST_MEMORY:-1Gi}"
FAST_CONCURRENCY="${FAST_CONCURRENCY:-1}"
FAST_MAX_INSTANCES="${FAST_MAX_INSTANCES:-5}"

MENU_CPU="${MENU_CPU:-2}"
MENU_MEMORY="${MENU_MEMORY:-4Gi}"
MENU_CONCURRENCY="${MENU_CONCURRENCY:-1}"
MENU_MAX_INSTANCES="${MENU_MAX_INSTANCES:-2}"

TASKS=(process_location pipeline details_enrich emoji photos menu_vibe vibe_reprocess)
FAST_TASKS=(pipeline details_enrich emoji photos vibe_reprocess)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "${SCRIPT_DIR}/.env" ]; then
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/.env"
else
  echo "❌ Error: .env file not found at ${SCRIPT_DIR}/.env"
  exit 1
fi

if [ -z "${SUPABASE_URL:-}" ]; then
  echo "❌ Error: SUPABASE_URL is not set in .env"
  exit 1
fi

case "${MAX_DELIVERY_ATTEMPTS}" in
  ''|*[!0-9]*)
    echo "❌ PUBSUB_MAX_DELIVERY_ATTEMPTS must be an integer between 5 and 100."
    exit 1
    ;;
esac
if [ "${MAX_DELIVERY_ATTEMPTS}" -lt 5 ] || [ "${MAX_DELIVERY_ATTEMPTS}" -gt 100 ]; then
  echo "❌ PUBSUB_MAX_DELIVERY_ATTEMPTS must be between 5 and 100."
  exit 1
fi

ensure_topic() {
  local topic="$1"
  if ! gcloud pubsub topics describe "${topic}" --project "${PROJECT}" >/dev/null 2>&1; then
    echo "ℹ️  Creating Pub/Sub topic: ${topic}"
    gcloud pubsub topics create "${topic}" --project "${PROJECT}"
  fi
}

has_enabled_secret_version() {
  local secret_name="$1"
  gcloud secrets versions list "${secret_name}" \
    --project "${PROJECT}" \
    --filter='state:ENABLED' \
    --format='value(name)' \
    --limit=1 | grep -q .
}

build_env_vars_from_dotenv() {
  local out=""
  while IFS= read -r line || [ -n "${line}" ]; do
    case "${line}" in
      ""|\#*) continue ;;
    esac
    line="${line#export }"

    if [[ "${line}" =~ ^([A-Za-z_][A-Za-z0-9_]*)= ]]; then
      local key="${BASH_REMATCH[1]}"
      case "${key}" in
        SUPABASE_SERVICE_KEY|SUPABASE_SERVICE_ROLE_KEY|SUPABASE_SECRET_KEY|GOOGLE_PLACE_API_KEY|REDIS_PASSWORD|XAI_API_KEY)
          continue
          ;;
        GOOGLE_CLOUD_PROJECT|PUBSUB_ENABLED|PUBSUB_TOPIC_LOCATION_TASKS|PUBSUB_PROJECT_ID)
          continue
          ;;
      esac

      local value="${!key:-}"
      value="${value//\\/\\\\}"
      value="${value//,/\\,}"
      out+="${out:+,}${key}=${value}"
    fi
  done < "${SCRIPT_DIR}/.env"
  echo "${out}"
}

validate_existing_subscription() {
  local subscription="$1"
  local task="$2"
  local expected_filter="attributes.task_type=\"${task}\""
  local actual_filter
  local ordering

  actual_filter="$(gcloud pubsub subscriptions describe "${subscription}" \
    --project "${PROJECT}" --format='value(filter)')"
  ordering="$(gcloud pubsub subscriptions describe "${subscription}" \
    --project "${PROJECT}" --format='value(enableMessageOrdering)')"

  if [ "${actual_filter}" != "${expected_filter}" ]; then
    echo "❌ Existing subscription ${subscription} has filter '${actual_filter}'; expected '${expected_filter}'."
    echo "   Filters are immutable; inspect this subscription before recreating it."
    exit 1
  fi
  if [ "${ordering}" != "True" ] && [ "${ordering}" != "true" ]; then
    echo "❌ Existing subscription ${subscription} does not have message ordering enabled."
    echo "   Ordering is fixed at creation; inspect this subscription before recreating it."
    exit 1
  fi
}

configure_task_subscription() {
  local task="$1"
  local worker_url="$2"
  local push_service_account="$3"
  local pubsub_service_agent="$4"
  local subscription="${TOPIC}-${task}"
  local endpoint="${worker_url}/internal/pubsub/location-tasks"
  local filter="attributes.task_type=\"${task}\""

  if gcloud pubsub subscriptions describe "${subscription}" --project "${PROJECT}" >/dev/null 2>&1; then
    validate_existing_subscription "${subscription}" "${task}"
    echo "ℹ️  Updating Pub/Sub subscription: ${subscription}"
    gcloud pubsub subscriptions update "${subscription}" \
      --project "${PROJECT}" \
      --ack-deadline "${ACK_DEADLINE_SECONDS}" \
      --expiration-period never \
      --min-retry-delay "${MIN_RETRY_DELAY}" \
      --max-retry-delay "${MAX_RETRY_DELAY}" \
      --dead-letter-topic "${DEAD_LETTER_TOPIC}" \
      --max-delivery-attempts "${MAX_DELIVERY_ATTEMPTS}" \
      --push-endpoint "${endpoint}" \
      --push-auth-service-account "${push_service_account}" \
      --push-auth-token-audience "${worker_url}"
  else
    echo "ℹ️  Creating Pub/Sub subscription: ${subscription}"
    gcloud pubsub subscriptions create "${subscription}" \
      --project "${PROJECT}" \
      --topic "${TOPIC}" \
      --message-filter "${filter}" \
      --enable-message-ordering \
      --ack-deadline "${ACK_DEADLINE_SECONDS}" \
      --expiration-period never \
      --min-retry-delay "${MIN_RETRY_DELAY}" \
      --max-retry-delay "${MAX_RETRY_DELAY}" \
      --dead-letter-topic "${DEAD_LETTER_TOPIC}" \
      --max-delivery-attempts "${MAX_DELIVERY_ATTEMPTS}" \
      --push-endpoint "${endpoint}" \
      --push-auth-service-account "${push_service_account}" \
      --push-auth-token-audience "${worker_url}"
  fi

  gcloud pubsub subscriptions add-iam-policy-binding "${subscription}" \
    --project "${PROJECT}" \
    --member "serviceAccount:${pubsub_service_agent}" \
    --role "roles/pubsub.subscriber" >/dev/null
}

echo "🔧 Enabling Pub/Sub API and provisioning topics..."
gcloud services enable pubsub.googleapis.com --project "${PROJECT}"
ensure_topic "${TOPIC}"
ensure_topic "${DEAD_LETTER_TOPIC}"

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
PUBSUB_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member "serviceAccount:${PUBSUB_SERVICE_AGENT}" \
  --role "roles/iam.serviceAccountTokenCreator" >/dev/null
gcloud pubsub topics add-iam-policy-binding "${DEAD_LETTER_TOPIC}" \
  --project "${PROJECT}" \
  --member "serviceAccount:${PUBSUB_SERVICE_AGENT}" \
  --role "roles/pubsub.publisher" >/dev/null

API_SA="$(gcloud run services describe "${API_SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" \
  --format='value(spec.template.spec.serviceAccountName)')"
if [ -z "${API_SA}" ]; then
  echo "❌ Could not resolve the API Cloud Run service account."
  exit 1
fi

gcloud pubsub topics add-iam-policy-binding "${TOPIC}" \
  --project "${PROJECT}" \
  --member "serviceAccount:${API_SA}" \
  --role "roles/pubsub.publisher" >/dev/null

if gcloud secrets describe "supabase-service-key" --project "${PROJECT}" >/dev/null 2>&1 && has_enabled_secret_version "supabase-service-key"; then
  SUPABASE_SERVICE_SECRET_NAME="supabase-service-key"
elif gcloud secrets describe "supabase-service-role-key" --project "${PROJECT}" >/dev/null 2>&1 && has_enabled_secret_version "supabase-service-role-key"; then
  SUPABASE_SERVICE_SECRET_NAME="supabase-service-role-key"
else
  echo "❌ No enabled Supabase service-key secret was found."
  exit 1
fi

FAST_SECRETS="SUPABASE_SERVICE_KEY=${SUPABASE_SERVICE_SECRET_NAME}:latest"
FAST_SECRETS+=",GOOGLE_PLACE_API_KEY=google-place-api-key:latest"
FAST_SECRETS+=",REDIS_PASSWORD=redis-password:latest"
MENU_SECRETS="${FAST_SECRETS}"

for required_secret in google-place-api-key redis-password; do
  if ! gcloud secrets describe "${required_secret}" --project "${PROJECT}" >/dev/null 2>&1 || ! has_enabled_secret_version "${required_secret}"; then
    echo "❌ Secret ${required_secret} has no enabled version."
    exit 1
  fi
done

XAI_SECRET_ENABLED="false"
if gcloud secrets describe "xai-api-key" --project "${PROJECT}" >/dev/null 2>&1 && has_enabled_secret_version "xai-api-key"; then
  XAI_SECRET_ENABLED="true"
  FAST_SECRETS+=",XAI_API_KEY=xai-api-key:latest"
  MENU_SECRETS+=",XAI_API_KEY=xai-api-key:latest"
elif [ -z "${XAI_API_KEY:-}" ]; then
  echo "❌ Secret xai-api-key has no enabled version and XAI_API_KEY is not set."
  exit 1
fi

: "${WARM_CACHE_ENABLED:=true}"
: "${WARM_CACHE_INTERVAL_SECONDS:=900}"
: "${WARM_CACHE_ZONE_SET:=london}"
: "${WARM_CACHE_START_HOUR:=9}"
: "${WARM_CACHE_END_HOUR:=21}"
: "${WARM_CACHE_TIMEZONE:=Europe/London}"
: "${CACHE_UNFILTERED_TTL:=3600}"

ENV_VARS="$(build_env_vars_from_dotenv)"
ENV_VARS+="${ENV_VARS:+,}GOOGLE_CLOUD_PROJECT=${PROJECT}"
ENV_VARS+=",PUBSUB_ENABLED=true"
ENV_VARS+=",PUBSUB_TOPIC_LOCATION_TASKS=${TOPIC}"
ENV_VARS+=",PUBSUB_PROJECT_ID=${PROJECT}"
ENV_VARS+=",WARM_CACHE_ENABLED=${WARM_CACHE_ENABLED}"
ENV_VARS+=",WARM_CACHE_INTERVAL_SECONDS=${WARM_CACHE_INTERVAL_SECONDS}"
ENV_VARS+=",WARM_CACHE_ZONE_SET=${WARM_CACHE_ZONE_SET}"
ENV_VARS+=",WARM_CACHE_START_HOUR=${WARM_CACHE_START_HOUR}"
ENV_VARS+=",WARM_CACHE_END_HOUR=${WARM_CACHE_END_HOUR}"
ENV_VARS+=",WARM_CACHE_TIMEZONE=${WARM_CACHE_TIMEZONE}"
ENV_VARS+=",CACHE_UNFILTERED_TTL=${CACHE_UNFILTERED_TTL}"

if [ "${XAI_SECRET_ENABLED}" != "true" ] && [ -n "${XAI_API_KEY:-}" ]; then
  escaped_xai="${XAI_API_KEY//\\/\\\\}"
  escaped_xai="${escaped_xai//,/\\,}"
  ENV_VARS+=",XAI_API_KEY=${escaped_xai}"
fi

IMAGE="europe-west2-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/pinit-recommendations:latest"
RUN_SA="${API_SA}"

echo "🚀 Deploying request-based location workers..."
gcloud run deploy "${WORKER_FAST_SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --image "${IMAGE}" \
  --service-account "${RUN_SA}" \
  --no-allow-unauthenticated \
  --cpu-throttling \
  --min-instances 0 \
  --timeout 900 \
  --cpu "${FAST_CPU}" \
  --memory "${FAST_MEMORY}" \
  --concurrency "${FAST_CONCURRENCY}" \
  --max-instances "${FAST_MAX_INSTANCES}" \
  --set-env-vars "${ENV_VARS}" \
  --set-secrets "${FAST_SECRETS}" \
  --command uvicorn \
  --args "pinit.worker.main:app,--host,0.0.0.0,--port,8080"

gcloud run deploy "${WORKER_MENU_SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --image "${IMAGE}" \
  --service-account "${RUN_SA}" \
  --no-allow-unauthenticated \
  --cpu-throttling \
  --min-instances 0 \
  --timeout 900 \
  --cpu "${MENU_CPU}" \
  --memory "${MENU_MEMORY}" \
  --concurrency "${MENU_CONCURRENCY}" \
  --max-instances "${MENU_MAX_INSTANCES}" \
  --set-env-vars "${ENV_VARS}" \
  --set-secrets "${MENU_SECRETS}" \
  --command uvicorn \
  --args "pinit.worker.main:app,--host,0.0.0.0,--port,8080"

PUSH_SA="${RUN_SA}"
gcloud run services add-iam-policy-binding "${WORKER_FAST_SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" \
  --member "serviceAccount:${PUSH_SA}" \
  --role "roles/run.invoker" >/dev/null
gcloud run services add-iam-policy-binding "${WORKER_MENU_SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" \
  --member "serviceAccount:${PUSH_SA}" \
  --role "roles/run.invoker" >/dev/null

FAST_WORKER_URL="$(gcloud run services describe "${WORKER_FAST_SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" --format='value(status.url)')"
MENU_WORKER_URL="$(gcloud run services describe "${WORKER_MENU_SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" --format='value(status.url)')"

for TASK in "${FAST_TASKS[@]}"; do
  configure_task_subscription "${TASK}" "${FAST_WORKER_URL}" "${PUSH_SA}" "${PUBSUB_SERVICE_AGENT}"
done
configure_task_subscription "menu_vibe" "${MENU_WORKER_URL}" "${PUSH_SA}" "${PUBSUB_SERVICE_AGENT}"
configure_task_subscription "process_location" "${MENU_WORKER_URL}" "${PUSH_SA}" "${PUBSUB_SERVICE_AGENT}"

if ! gcloud pubsub subscriptions describe "${DEAD_LETTER_SUBSCRIPTION}" --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud pubsub subscriptions create "${DEAD_LETTER_SUBSCRIPTION}" \
    --project "${PROJECT}" \
    --topic "${DEAD_LETTER_TOPIC}" \
    --expiration-period never \
    --message-retention-duration 14d
fi

echo "🔎 Verifying workers and subscriptions before enabling API publishing..."
EXPECT_API_PUBSUB=false "${SCRIPT_DIR}/verify_pubsub.sh"

echo "🔁 Enabling Pub/Sub publishing on the request-based API service..."
gcloud run services update "${API_SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --update-env-vars "PUBSUB_ENABLED=true,PUBSUB_TOPIC_LOCATION_TASKS=${TOPIC},PUBSUB_PROJECT_ID=${PROJECT},GOOGLE_CLOUD_PROJECT=${PROJECT}" \
  --cpu-throttling \
  --min-instances 0

EXPECT_API_PUBSUB=true "${SCRIPT_DIR}/verify_pubsub.sh"

echo "✅ Durable Pub/Sub location processing deployed."
echo "   Topic: ${TOPIC}"
echo "   Dead-letter topic: ${DEAD_LETTER_TOPIC}"
echo "   Dead-letter inspection subscription: ${DEAD_LETTER_SUBSCRIPTION}"
echo "   Fast worker: ${FAST_WORKER_URL}"
echo "   Menu worker: ${MENU_WORKER_URL}"
