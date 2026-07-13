# Deploying Durable Location Processing

This runbook enables the existing Pub/Sub location workers in `pinit-494520` without permanent Cloud Run CPU. The API and both workers use request-based billing (`--cpu-throttling`) and zero minimum instances.

## Prerequisites

Run from the repository root with a clean, committed revision:

```bash
gcloud auth list
gcloud config set project pinit-494520
gcloud config get-value project
git status --short
```

The active account must be able to deploy Cloud Run services, administer Pub/Sub topics/subscriptions, update IAM, use Artifact Registry, and access the required Secret Manager bindings.

Confirm the required secrets have enabled versions:

```bash
for secret in supabase-service-key google-place-api-key redis-password xai-api-key; do
  gcloud secrets versions list "$secret" \
    --project pinit-494520 \
    --filter='state:ENABLED' \
    --limit=1 \
    --format='value(name)'
done
```

If `supabase-service-key` does not exist, the scripts also support the legacy `supabase-service-role-key` secret.

## Record the current state

```bash
for service in pinit-recommendations-api pinit-location-worker pinit-location-worker-menu; do
  gcloud run services describe "$service" \
    --project pinit-494520 \
    --region europe-west2 \
    --format='table(metadata.name,status.latestReadyRevisionName,spec.template.metadata.annotations)'
done

gcloud pubsub subscriptions list \
  --project pinit-494520 \
  --filter='topic:location-tasks' \
  --format='table(name.basename(),pushConfig.pushEndpoint,ackDeadlineSeconds,filter)'
```

An absent `run.googleapis.com/cpu-throttling` annotation means request-based billing; after this deployment it should be explicitly `true`. `autoscaling.knative.dev/minScale` must be absent or `0`.

## Deploy

The supported entry point builds one image, deploys the API with Pub/Sub temporarily disabled, provisions the durable worker system, verifies it, and enables API publishing last:

```bash
DEPLOY_PUBSUB=true ./deploy.sh
```

The deployment creates or updates:

- `location-tasks`
- `location-tasks-dead-letter`
- six filtered authenticated push subscriptions
- `location-tasks-dead-letter-inspect`
- `pinit-location-worker`
- `pinit-location-worker-menu`
- required publisher, subscriber, token-creator, and Cloud Run invoker IAM bindings

Every source subscription retries from 10 seconds up to 600 seconds and forwards a message to the dead-letter topic after ten delivery attempts.

## Verify configuration

The verifier is read-only:

```bash
./verify_pubsub.sh
```

It fails if any service is not ready, has non-request-based CPU, has a nonzero minimum scale, or if subscriptions/IAM differ from the required contract.

## Verify private worker health

```bash
PROJECT=pinit-494520
REGION=europe-west2
PUSH_SA="$(gcloud run services describe pinit-recommendations-api \
  --project "$PROJECT" --region "$REGION" \
  --format='value(spec.template.spec.serviceAccountName)')"

for service in pinit-location-worker pinit-location-worker-menu; do
  url="$(gcloud run services describe "$service" \
    --project "$PROJECT" --region "$REGION" \
    --format='value(status.url)')"
  token="$(gcloud auth print-identity-token \
    --impersonate-service-account "$PUSH_SA" \
    --audiences "$url" \
    --include-email)"
  curl --fail --silent --show-error \
    -H "Authorization: Bearer $token" \
    "$url/health"
done
```

Both requests must return `{"status":"ok"}`.

## Controlled end-to-end smoke test

Use an existing fully processed row whose `updated_vibe` is true. This exercises API publish, Pub/Sub delivery, worker consume, and acknowledgement while the pipeline itself takes its existing no-op branch.

Set its Google Place ID without printing credentials:

```bash
export GOOGLE_PLACE_ID='<existing fully processed Google Place ID>'
export REQUEST_MARKER="pubsub-smoke-$(date -u +%Y%m%dT%H%M%SZ)"
export API_URL='https://pinit-recommendations-api-jkqbw4i75a-nw.a.run.app'

curl --fail --silent --show-error \
  -X POST "$API_URL/locations/add" \
  -H 'Content-Type: application/json' \
  -H "X-Smoke-Marker: $REQUEST_MARKER" \
  --data "{\"google_place_id\":\"$GOOGLE_PLACE_ID\",\"source\":\"in-app\",\"classify_photo\":false,\"generate_emoji\":false}"
```

Then inspect recent API and worker logs. The API response does not currently echo the internal task request ID, so correlate using the location ID and timestamp:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="pinit-recommendations-api" AND textPayload:"Published Pub/Sub task pipeline"' \
  --project pinit-494520 \
  --freshness 15m \
  --limit 20 \
  --order desc \
  --format='value(timestamp,textPayload)'

gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="pinit-location-worker" AND (textPayload:"Consumed Pub/Sub message" OR textPayload:"Ack Pub/Sub message")' \
  --project pinit-494520 \
  --freshness 15m \
  --limit 40 \
  --order asc \
  --format='value(timestamp,textPayload)'
```

The same location ID and task request ID must appear in the API publish, worker consume, and worker acknowledgement messages.

## Inspect dead letters

```bash
gcloud pubsub subscriptions pull location-tasks-dead-letter-inspect \
  --project pinit-494520 \
  --limit 10 \
  --format='table(message.messageId,message.attributes,message.publishTime)'
```

Do not use `--auto-ack` until a failed message has been investigated. Re-running `./verify_pubsub.sh` confirms subscription policies, but does not consume any messages.

## Roll back safely

First stop new Pub/Sub publishing while preserving request-based CPU:

```bash
gcloud run services update pinit-recommendations-api \
  --project pinit-494520 \
  --region europe-west2 \
  --update-env-vars PUBSUB_ENABLED=false \
  --cpu-throttling \
  --min-instances 0
```

Then convert the six source subscriptions from push to pull. Messages remain retained instead of repeatedly invoking workers:

```bash
for task in pipeline details_enrich emoji photos menu_vibe vibe_reprocess; do
  gcloud pubsub subscriptions modify-push-config "location-tasks-$task" \
    --project pinit-494520 \
    --clear-push-config
done
```

Confirm the API fallback started:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="pinit-recommendations-api" AND textPayload:"Pub/Sub disabled"' \
  --project pinit-494520 \
  --freshness 15m \
  --limit 10 \
  --format='value(timestamp,textPayload)'
```

Do not delete or acknowledge retained source/dead-letter messages until they have been inspected. Re-running `DEPLOY_PUBSUB=true ./deploy.sh` restores authenticated push delivery after the underlying issue is resolved.
