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
EXPECT_API_PUBSUB="${EXPECT_API_PUBSUB:-true}"

TASKS=(pipeline details_enrich emoji photos menu_vibe vibe_reprocess)
FAST_TASKS=(pipeline details_enrich emoji photos vibe_reprocess)

fail() {
  echo "❌ $*" >&2
  exit 1
}

policy_has_binding() {
  local policy_json="$1"
  local role="$2"
  local member="$3"
  POLICY_JSON="${policy_json}" ROLE="${role}" MEMBER="${member}" python3 - <<'PY'
import json
import os
import sys

policy = json.loads(os.environ["POLICY_JSON"] or "{}")
role = os.environ["ROLE"]
member = os.environ["MEMBER"]
for binding in policy.get("bindings", []):
    if binding.get("role") == role and member in binding.get("members", []):
        sys.exit(0)
sys.exit(1)
PY
}

service_value() {
  local service_json="$1"
  local field="$2"
  SERVICE_JSON="${service_json}" FIELD="${field}" python3 - <<'PY'
import json
import os

service = json.loads(os.environ["SERVICE_JSON"])
template = service.get("spec", {}).get("template", {})
container = (template.get("spec", {}).get("containers") or [{}])[0]
field = os.environ["FIELD"]

if field == "url":
    print(service.get("status", {}).get("url", ""))
elif field == "service_account":
    print(template.get("spec", {}).get("serviceAccountName", ""))
elif field == "pubsub_enabled":
    values = {item.get("name"): item.get("value") for item in container.get("env", [])}
    print(values.get("PUBSUB_ENABLED", ""))
PY
}

verify_service() {
  local service="$1"
  local expected_pubsub="${2:-}"
  local service_json
  service_json="$(gcloud run services describe "${service}" \
    --project "${PROJECT}" --region "${REGION}" --format=json)"

  SERVICE_JSON="${service_json}" SERVICE_NAME="${service}" EXPECTED_PUBSUB="${expected_pubsub}" python3 - <<'PY'
import json
import os
import sys

service = json.loads(os.environ["SERVICE_JSON"])
name = os.environ["SERVICE_NAME"]
expected_pubsub = os.environ["EXPECTED_PUBSUB"]
status = service.get("status", {})
ready = next(
    (item.get("status") for item in status.get("conditions", []) if item.get("type") == "Ready"),
    None,
)
if ready != "True":
    raise SystemExit(f"{name}: Ready condition is {ready!r}")

template = service.get("spec", {}).get("template", {})
annotations = template.get("metadata", {}).get("annotations", {})
cpu_throttling = annotations.get("run.googleapis.com/cpu-throttling", "true")
if str(cpu_throttling).lower() != "true":
    raise SystemExit(f"{name}: cpu-throttling is {cpu_throttling!r}; request-based billing required")

min_scale = annotations.get("autoscaling.knative.dev/minScale", "0")
if str(min_scale or "0") != "0":
    raise SystemExit(f"{name}: minScale is {min_scale!r}; zero required")

if expected_pubsub:
    container = (template.get("spec", {}).get("containers") or [{}])[0]
    env = {item.get("name"): item.get("value") for item in container.get("env", [])}
    actual = str(env.get("PUBSUB_ENABLED", "")).lower()
    if actual != expected_pubsub.lower():
        raise SystemExit(
            f"{name}: PUBSUB_ENABLED is {actual!r}; expected {expected_pubsub.lower()!r}"
        )
PY

  echo "✅ ${service}: ready, request-based CPU, minScale=0"
}

verify_subscription() {
  local task="$1"
  local worker_url="$2"
  local push_service_account="$3"
  local subscription="${TOPIC}-${task}"
  local subscription_json
  subscription_json="$(gcloud pubsub subscriptions describe "${subscription}" \
    --project "${PROJECT}" --format=json)"

  SUBSCRIPTION_JSON="${subscription_json}" \
  TASK="${task}" \
  WORKER_URL="${worker_url}" \
  PUSH_SERVICE_ACCOUNT="${push_service_account}" \
  PROJECT="${PROJECT}" \
  DEAD_LETTER_TOPIC="${DEAD_LETTER_TOPIC}" \
  MIN_RETRY_DELAY="${MIN_RETRY_DELAY}" \
  MAX_RETRY_DELAY="${MAX_RETRY_DELAY}" \
  MAX_DELIVERY_ATTEMPTS="${MAX_DELIVERY_ATTEMPTS}" \
  ACK_DEADLINE_SECONDS="${ACK_DEADLINE_SECONDS}" \
  python3 - <<'PY'
import json
import os

subscription = json.loads(os.environ["SUBSCRIPTION_JSON"])
task = os.environ["TASK"]
worker_url = os.environ["WORKER_URL"]
expected_filter = f'attributes.task_type="{task}"'
expected_endpoint = f"{worker_url}/internal/pubsub/location-tasks"
expected_dlq = (
    f"projects/{os.environ['PROJECT']}/topics/{os.environ['DEAD_LETTER_TOPIC']}"
)

def seconds(value: str) -> int:
    return int(str(value).removesuffix("s"))

if subscription.get("filter") != expected_filter:
    raise SystemExit(f"{task}: filter mismatch: {subscription.get('filter')!r}")
if subscription.get("enableMessageOrdering") is not True:
    raise SystemExit(f"{task}: message ordering is not enabled")
if int(subscription.get("ackDeadlineSeconds", 0)) != int(os.environ["ACK_DEADLINE_SECONDS"]):
    raise SystemExit(f"{task}: acknowledgement deadline mismatch")

push = subscription.get("pushConfig") or {}
oidc = push.get("oidcToken") or {}
if push.get("pushEndpoint") != expected_endpoint:
    raise SystemExit(f"{task}: push endpoint mismatch: {push.get('pushEndpoint')!r}")
if oidc.get("serviceAccountEmail") != os.environ["PUSH_SERVICE_ACCOUNT"]:
    raise SystemExit(f"{task}: push service account mismatch")
if oidc.get("audience") != worker_url:
    raise SystemExit(f"{task}: push token audience mismatch")

retry = subscription.get("retryPolicy") or {}
if seconds(retry.get("minimumBackoff", "0s")) != seconds(os.environ["MIN_RETRY_DELAY"]):
    raise SystemExit(f"{task}: minimum retry delay mismatch")
if seconds(retry.get("maximumBackoff", "0s")) != seconds(os.environ["MAX_RETRY_DELAY"]):
    raise SystemExit(f"{task}: maximum retry delay mismatch")

dead_letter = subscription.get("deadLetterPolicy") or {}
if dead_letter.get("deadLetterTopic") != expected_dlq:
    raise SystemExit(f"{task}: dead-letter topic mismatch")
if int(dead_letter.get("maxDeliveryAttempts", 0)) != int(os.environ["MAX_DELIVERY_ATTEMPTS"]):
    raise SystemExit(f"{task}: max delivery attempts mismatch")
PY

  echo "✅ ${subscription}: push, ordering, retryPolicy, deadLetterPolicy"
}

echo "🔎 Verifying durable Pub/Sub location processing in ${PROJECT}/${REGION}..."

verify_service "${API_SERVICE}" "${EXPECT_API_PUBSUB}"
verify_service "${WORKER_FAST_SERVICE}"
verify_service "${WORKER_MENU_SERVICE}"

API_JSON="$(gcloud run services describe "${API_SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" --format=json)"
FAST_JSON="$(gcloud run services describe "${WORKER_FAST_SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" --format=json)"
MENU_JSON="$(gcloud run services describe "${WORKER_MENU_SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" --format=json)"

PUSH_SA="$(service_value "${API_JSON}" service_account)"
FAST_WORKER_URL="$(service_value "${FAST_JSON}" url)"
MENU_WORKER_URL="$(service_value "${MENU_JSON}" url)"
[ -n "${PUSH_SA}" ] || fail "API service account is empty"
[ -n "${FAST_WORKER_URL}" ] || fail "Fast worker URL is empty"
[ -n "${MENU_WORKER_URL}" ] || fail "Menu worker URL is empty"

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
PUBSUB_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"

PROJECT_POLICY="$(gcloud projects get-iam-policy "${PROJECT}" --format=json)"
policy_has_binding "${PROJECT_POLICY}" \
  "roles/iam.serviceAccountTokenCreator" \
  "serviceAccount:${PUBSUB_SERVICE_AGENT}" || fail "Pub/Sub service agent lacks token creator"

TOPIC_POLICY="$(gcloud pubsub topics get-iam-policy "${TOPIC}" \
  --project "${PROJECT}" --format=json)"
policy_has_binding "${TOPIC_POLICY}" \
  "roles/pubsub.publisher" \
  "serviceAccount:${PUSH_SA}" || fail "API service account lacks topic publisher"

DLQ_POLICY="$(gcloud pubsub topics get-iam-policy "${DEAD_LETTER_TOPIC}" \
  --project "${PROJECT}" --format=json)"
policy_has_binding "${DLQ_POLICY}" \
  "roles/pubsub.publisher" \
  "serviceAccount:${PUBSUB_SERVICE_AGENT}" || fail "Pub/Sub service agent lacks dead-letter publisher"

for TASK in "${FAST_TASKS[@]}"; do
  verify_subscription "${TASK}" "${FAST_WORKER_URL}" "${PUSH_SA}"
done
verify_subscription "menu_vibe" "${MENU_WORKER_URL}" "${PUSH_SA}"

for TASK in "${TASKS[@]}"; do
  SUBSCRIPTION="${TOPIC}-${TASK}"
  SUB_POLICY="$(gcloud pubsub subscriptions get-iam-policy "${SUBSCRIPTION}" \
    --project "${PROJECT}" --format=json)"
  policy_has_binding "${SUB_POLICY}" \
    "roles/pubsub.subscriber" \
    "serviceAccount:${PUBSUB_SERVICE_AGENT}" || fail "${SUBSCRIPTION}: service agent lacks subscriber"
done

DLQ_SUB_JSON="$(gcloud pubsub subscriptions describe "${DEAD_LETTER_SUBSCRIPTION}" \
  --project "${PROJECT}" --format=json)"
DLQ_SUBSCRIPTION_JSON="${DLQ_SUB_JSON}" PROJECT="${PROJECT}" DEAD_LETTER_TOPIC="${DEAD_LETTER_TOPIC}" python3 - <<'PY'
import json
import os

subscription = json.loads(os.environ["DLQ_SUBSCRIPTION_JSON"])
expected = f"projects/{os.environ['PROJECT']}/topics/{os.environ['DEAD_LETTER_TOPIC']}"
if subscription.get("topic") != expected:
    raise SystemExit("dead-letter inspection subscription points to the wrong topic")
if subscription.get("pushConfig", {}).get("pushEndpoint"):
    raise SystemExit("dead-letter inspection subscription must remain pull-based")
PY

for SERVICE in "${WORKER_FAST_SERVICE}" "${WORKER_MENU_SERVICE}"; do
  RUN_POLICY="$(gcloud run services get-iam-policy "${SERVICE}" \
    --project "${PROJECT}" --region "${REGION}" --format=json)"
  policy_has_binding "${RUN_POLICY}" \
    "roles/run.invoker" \
    "serviceAccount:${PUSH_SA}" || fail "${SERVICE}: push identity lacks run.invoker"
done

echo "✅ IAM: authenticated push and dead-letter forwarding"
echo "✅ ${DEAD_LETTER_SUBSCRIPTION}: retained pull inspection subscription"
echo "✅ Durable location-processing infrastructure verified"
