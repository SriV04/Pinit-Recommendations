# Docker Deployment Guide

This guide explains how to build and deploy the Pinit Recommendations API using Docker.

## Prerequisites

- Docker installed (https://docs.docker.com/get-docker/)
- Docker Compose installed (comes with Docker Desktop)
- `.env` file with Supabase credentials

## Quick Start

### Using Docker Compose (Recommended)

1. **Build and start the container:**
```bash
docker-compose up --build
```

2. **Run in detached mode (background):**
```bash
docker-compose up -d
```

3. **View logs:**
```bash
docker-compose logs -f
```

4. **Stop the container:**
```bash
docker-compose down
```

### Using Docker directly

1. **Build the image:**
```bash
docker build -t pinit-recommendations-api .
```

2. **Run the container:**
```bash
docker run -d \
  --name pinit-api \
  -p 8000:8000 \
  --env-file .env \
  pinit-recommendations-api
```

3. **View logs:**
```bash
docker logs -f pinit-api
```

4. **Stop the container:**
```bash
docker stop pinit-api
docker rm pinit-api
```

## Accessing the API

Once running, the API is available at:
- API: http://localhost:8000
- Interactive docs: http://localhost:8000/docs
- Health check: http://localhost:8000/health

## Environment Variables

Required environment variables in `.env`:
```
SUPABASE_URL=your_supabase_url
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key
```

## Development vs Production

### Development Mode
Docker Compose mounts local directories as volumes so code changes are reflected immediately:
```yaml
volumes:
  - ./src:/app/src
  - ./data:/app/data
```

### Production Mode
For production, comment out the volume mounts in `docker-compose.yml` to bake everything into the image.

## Building for Production

1. **Remove development volumes from docker-compose.yml**

2. **Build production image:**
```bash
docker-compose build --no-cache
```

3. **Tag for registry:**
```bash
docker tag pinit-recommendations-api your-registry/pinit-recommendations-api:v1.0.0
```

4. **Push to registry:**
```bash
docker push your-registry/pinit-recommendations-api:v1.0.0
```

## Cloud Deployment

### AWS ECS

1. **Push image to ECR:**
```bash
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin your-account.dkr.ecr.us-east-1.amazonaws.com
docker tag pinit-recommendations-api:latest your-account.dkr.ecr.us-east-1.amazonaws.com/pinit-api:latest
docker push your-account.dkr.ecr.us-east-1.amazonaws.com/pinit-api:latest
```

2. **Create ECS task definition with environment variables**

3. **Create ECS service exposing port 8000**

### Google Cloud Run

```bash
# Build and deploy in one command
gcloud run deploy pinit-recommendations-api \
  --source . \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars SUPABASE_URL=your_url,SUPABASE_SERVICE_ROLE_KEY=your_key
```

### Azure Container Instances

```bash
az container create \
  --resource-group myResourceGroup \
  --name pinit-api \
  --image your-registry/pinit-recommendations-api:latest \
  --dns-name-label pinit-api \
  --ports 8000 \
  --environment-variables SUPABASE_URL=your_url SUPABASE_SERVICE_ROLE_KEY=your_key
```

### Railway

1. Connect your GitHub repository
2. Add environment variables in Railway dashboard
3. Railway auto-detects Dockerfile and deploys

### Heroku

```bash
heroku container:login
heroku create pinit-recommendations-api
heroku container:push web -a pinit-recommendations-api
heroku container:release web -a pinit-recommendations-api
heroku config:set SUPABASE_URL=your_url SUPABASE_SERVICE_ROLE_KEY=your_key -a pinit-recommendations-api
```

## Health Checks

The Docker container includes a health check that pings `/health` endpoint every 30 seconds:
```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3
```

Monitor health status:
```bash
docker ps
# Look for "healthy" status
```

## Troubleshooting

### Container won't start
```bash
# Check logs
docker-compose logs

# Check if port 8000 is already in use
lsof -i :8000

# Rebuild without cache
docker-compose build --no-cache
```

### Data not loading
```bash
# Ensure data directory exists and has files
ls -la data/raw/

# Check environment variables
docker exec pinit-api env | grep SUPABASE
```

### Memory issues
```bash
# Increase Docker memory in Docker Desktop settings
# Or limit container memory in docker-compose.yml:
services:
  api:
    deploy:
      resources:
        limits:
          memory: 2G
```

## Performance Tuning

### Multi-stage build (reduces image size)
Add to Dockerfile:
```dockerfile
FROM python:3.12-slim as builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

FROM python:3.12-slim
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
WORKDIR /app
COPY . .
CMD ["uvicorn", "pinit.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Worker processes for production
```bash
# Use multiple uvicorn workers
CMD ["uvicorn", "pinit.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
```

## Security Best Practices

1. **Never commit .env file**
2. **Use secrets management in production** (AWS Secrets Manager, GCP Secret Manager, etc.)
3. **Run as non-root user:**
```dockerfile
RUN useradd -m -u 1000 appuser
USER appuser
```
4. **Scan for vulnerabilities:**
```bash
docker scan pinit-recommendations-api
```

## Monitoring

### Add Prometheus metrics
```bash
pip install prometheus-fastapi-instrumentator
```

### Log aggregation
Configure logging to stdout (already done) and use log aggregation service (CloudWatch, Datadog, etc.)

## Backup Strategy

Data is stored in Supabase, so no additional backup needed for the container. For local data:
```bash
# Backup data directory
docker cp pinit-api:/app/data ./backup/data-$(date +%Y%m%d)
```
