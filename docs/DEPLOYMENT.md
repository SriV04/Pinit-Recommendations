# Deployment Guide

## Local Development

### Run with Docker
```bash
# Build the image
docker build -t pinit-recommendations-api:latest .

# Run with environment variables from .env
docker run --env-file .env -e LOG_LEVEL=DEBUG -p 8080:8080 pinit-recommendations-api:latest

# Or use docker-compose
docker compose up --build
```

### Run without Docker
```bash
# Ensure Python environment is configured
python start_api.py
```

### Test locally
```bash
# Health check
curl http://localhost:8080/health

# API docs
open http://localhost:8080/docs

# Test recommendations
curl -X POST http://localhost:8080/recommendations/proximal \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "3b36081d-69f3-4d75-9db6-46ac6eaacc40",
    "latitude": 51.5074,
    "longitude": -0.1278,
    "radius_km": 10,
    "max_results": 20
  }'
```

---

## Cloud Deployment

### Prerequisites
- GCP project: `pinit-a97eb`
- Artifact Registry repository: `pinit-api`
- Region: `europe-west2`
- Service account: `pinit-api-sa@pinit-a97eb.iam.gserviceaccount.com`

### Environment Variables Required
Set these in Secret Manager or as env vars:
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `GOOGLE_MAPS_API_KEY`
- `GOOGLE_CLOUD_PROJECT`

### Deploy Script
```bash
#!/bin/bash
# deploy.sh - Build and deploy to Cloud Run

set -e  # Exit on error

PROJECT_ID="pinit-a97eb"
REGION="europe-west2"
SERVICE_NAME="pinit-recommendations-api"
IMAGE_NAME="europe-west2-docker.pkg.dev/${PROJECT_ID}/pinit-api/${SERVICE_NAME}:latest"

echo "🔨 Building image on Google Cloud Build..."
gcloud builds submit \
  --region=${REGION} \
  --tag=${IMAGE_NAME}

echo "🚀 Deploying to Cloud Run..."
gcloud run deploy ${SERVICE_NAME} \
  --image=${IMAGE_NAME} \
  --region=${REGION} \
  --set-env-vars=GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_MAPS_API_KEY=${GOOGLE_MAPS_API_KEY},SUPABASE_URL=${SUPABASE_URL},SUPABASE_SERVICE_ROLE_KEY=${SUPABASE_SERVICE_ROLE_KEY} \
  --service-account=pinit-api-sa@${PROJECT_ID}.iam.gserviceaccount.com \
  --allow-unauthenticated \
  --timeout=300 \
  --cpu=1 \
  --memory=512Mi

echo "✅ Deployment complete!"
echo "Service URL:"
gcloud run services describe ${SERVICE_NAME} --region=${REGION} --format='value(status.url)'
```

### Quick Deploy (copy-paste)
```bash
# Source your env vars first (or use Secret Manager)
export GOOGLE_MAPS_API_KEY="YOUR_KEY"
export SUPABASE_URL="https://umjoqvsfqhirysdjxnaf.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="YOUR_KEY"

# Build and deploy
gcloud builds submit \
  --region=europe-west2 \
  --tag=europe-west2-docker.pkg.dev/pinit-a97eb/pinit-api/pinit-recommendations-api:latest

gcloud run deploy pinit-recommendations-api \
  --image europe-west2-docker.pkg.dev/pinit-a97eb/pinit-api/pinit-recommendations-api:latest \
  --region europe-west2 \
  --set-env-vars=GOOGLE_CLOUD_PROJECT=pinit-a97eb \
  --service-account=pinit-api-sa@pinit-a97eb.iam.gserviceaccount.com \
  --allow-unauthenticated \
  --timeout=300 \
  --cpu=1 \
  --memory=512Mi
```

### Using Secret Manager (recommended for production)
```bash
# One-time setup: Create secrets
gcloud secrets create google-maps-api-key --replication-policy="automatic"
printf "YOUR_KEY" | gcloud secrets versions add google-maps-api-key --data-file=-

gcloud secrets create supabase-url --replication-policy="automatic"
printf "https://umjoqvsfqhirysdjxnaf.supabase.co" | gcloud secrets versions add supabase-url --data-file=-

gcloud secrets create supabase-service-role-key --replication-policy="automatic"
printf "YOUR_KEY" | gcloud secrets versions add supabase-service-role-key --data-file=-

# Deploy with secrets
gcloud builds submit \
  --region=europe-west2 \
  --tag=europe-west2-docker.pkg.dev/pinit-a97eb/pinit-api/pinit-recommendations-api:latest

gcloud run deploy pinit-recommendations-api \
  --image europe-west2-docker.pkg.dev/pinit-a97eb/pinit-api/pinit-recommendations-api:latest \
  --region europe-west2 \
  --set-secrets=GOOGLE_MAPS_API_KEY=google-maps-api-key:latest,SUPABASE_URL=supabase-url:latest,SUPABASE_SERVICE_ROLE_KEY=supabase-service-role-key:latest \
  --set-env-vars=GOOGLE_CLOUD_PROJECT=pinit-a97eb \
  --service-account=pinit-api-sa@pinit-a97eb.iam.gserviceaccount.com \
  --allow-unauthenticated \
  --timeout=300 \
  --cpu=1 \
  --memory=512Mi
```

---

## Cleanup

### Clean up old Docker images (saves storage costs)
```bash
# List all images with tags
gcloud artifacts docker images list \
  europe-west2-docker.pkg.dev/pinit-a97eb/pinit-api/pinit-recommendations-api \
  --include-tags

# Delete all except the latest 3 images
gcloud artifacts docker images list \
  europe-west2-docker.pkg.dev/pinit-a97eb/pinit-api/pinit-recommendations-api \
  --format="value(IMAGE)" \
  --sort-by="~CREATE_TIME" \
  | tail -n +4 \
  | xargs -I {} gcloud artifacts docker images delete {} --quiet

# Or delete a specific image
gcloud artifacts docker images delete \
  europe-west2-docker.pkg.dev/pinit-a97eb/pinit-api/pinit-recommendations-api@sha256:DIGEST \
  --quiet
```

### Clean up old Cloud Run revisions (optional - revisions are free when idle)
```bash
# List all revisions
gcloud run revisions list \
  --service=pinit-recommendations-api \
  --region=europe-west2

# Delete old revisions (keep latest 2)
gcloud run revisions list \
  --service=pinit-recommendations-api \
  --region=europe-west2 \
  --format="value(name)" \
  --sort-by="~metadata.creationTimestamp" \
  | tail -n +3 \
  | xargs -I {} gcloud run revisions delete {} --region=europe-west2 --quiet
```

### Set up automatic image cleanup policy
Create `cleanup-policy.json`:
```json
{
  "rules": [{
    "id": "keep-recent-images",
    "action": "KEEP",
    "mostRecentVersions": {
      "keepCount": 5
    }
  }]
}
```

Apply the policy:
```bash
gcloud artifacts repositories set-cleanup-policies pinit-api \
  --location=europe-west2 \
  --policy=cleanup-policy.json
```

### View storage usage
```bash
# Check repository size
gcloud artifacts repositories describe pinit-api \
  --location=europe-west2 \
  --format="value(sizeBytes.size(unit=1024*1024*1024))"

# List all repositories with sizes
gcloud artifacts repositories list \
  --format="table(name,format,sizeBytes.size(unit=1024*1024*1024):label=SIZE_GB)"
```

---

## Service Information

**Production URL:** https://pinit-recommendations-api-630839392908.europe-west2.run.app

**API Documentation:**
- Swagger UI: https://pinit-recommendations-api-630839392908.europe-west2.run.app/docs
- ReDoc: https://pinit-recommendations-api-630839392908.europe-west2.run.app/redoc

**Endpoints:**
- `GET /health` - Health check
- `POST /recommendations/proximal` - Get personalized recommendations
- `POST /recommendations/proximal/batch` - Batch recommendations
- `POST /locations/add` - Add new location by Google Place ID
- `GET /users` - List users
- `GET /users/{user_id}/profile` - Get user profile

---

## Monitoring

```bash
# View logs
gcloud run services logs read pinit-recommendations-api \
  --region=europe-west2 \
  --limit=50

# Stream logs in real-time
gcloud run services logs tail pinit-recommendations-api \
  --region=europe-west2

# View service details
gcloud run services describe pinit-recommendations-api \
  --region=europe-west2

# View metrics
gcloud run services describe pinit-recommendations-api \
  --region=europe-west2 \
  --format="value(status.traffic)"
```

---

## Troubleshooting

### Service fails to start
```bash
# Check logs for errors
gcloud run services logs read pinit-recommendations-api --region=europe-west2 --limit=100

# Verify env vars are set
gcloud run services describe pinit-recommendations-api --region=europe-west2 --format="value(spec.template.spec.containers[0].env)"

# Check service account permissions
gcloud projects get-iam-policy pinit-a97eb \
  --flatten="bindings[].members" \
  --filter="bindings.members:pinit-api-sa@pinit-a97eb.iam.gserviceaccount.com"
```

### Rebuild from scratch (no cache)
```bash
gcloud builds submit \
  --region=europe-west2 \
  --no-cache \
  --tag=europe-west2-docker.pkg.dev/pinit-a97eb/pinit-api/pinit-recommendations-api:latest
```

### Test specific revision
```bash
# Get revision name
gcloud run revisions list \
  --service=pinit-recommendations-api \
  --region=europe-west2

# Route traffic to specific revision
gcloud run services update-traffic pinit-recommendations-api \
  --region=europe-west2 \
  --to-revisions=REVISION_NAME=100
```
