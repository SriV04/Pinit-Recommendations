#!/bin/bash
set -e

PROJECT="project-add4b0f5-0080-47ef-80f"
REGION="europe-west2"
API_SERVICE="pinit-recommendations-api"
WORKER_SERVICE="pinit-location-worker"
TOPIC="location-tasks"

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
        SUPABASE_SERVICE_ROLE_KEY|GOOGLE_PLACE_API_KEY|REDIS_PASSWORD)
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

ENV_VARS="$(build_env_vars_from_dotenv)"
ENV_VARS+="${ENV_VARS:+,}GOOGLE_CLOUD_PROJECT=${PROJECT},PUBSUB_ENABLED=true,PUBSUB_TOPIC_LOCATION_TASKS=${TOPIC}"

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
SECRETS="SUPABASE_SERVICE_ROLE_KEY=supabase-service-role-key:latest"
SECRETS+=",GOOGLE_PLACE_API_KEY=google-place-api-key:latest"
SECRETS+=",REDIS_PASSWORD=redis-password:latest"

gcloud run deploy "${WORKER_SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --image "${IMAGE}" \
  --service-account "${RUN_SA}" \
  --no-allow-unauthenticated \
  --timeout 900 \
  --cpu 4 \
  --memory 4Gi \
  --concurrency 4 \
  --max-instances 30 \
  --set-env-vars "${WORKER_ENV_VARS}" \
  --set-secrets "${SECRETS}" \
  --command uvicorn \
  --args "pinit.worker.main:app,--host,0.0.0.0,--port,8080"


PUSH_SA="${RUN_SA}"

gcloud run services add-iam-policy-binding "${WORKER_SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --member "serviceAccount:${PUSH_SA}" \
  --role "roles/run.invoker"


WORKER_URL="$(gcloud run services describe "${WORKER_SERVICE}" --project "${PROJECT}" --region "${REGION}" --format='value(status.url)')"

for TASK in pipeline details_enrich emoji photos menu_vibe vibe_reprocess; do
  gcloud pubsub subscriptions update "${TOPIC}-${TASK}" \
    --project "${PROJECT}" \
    --push-endpoint "${WORKER_URL}/internal/pubsub/location-tasks" \
    --push-auth-service-account "${PUSH_SA}"
done
