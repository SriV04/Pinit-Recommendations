#!/bin/sh
# Entrypoint script for Cloud Run deployment

# Use PORT env var from Cloud Run, default to 8080
PORT=${PORT:-8080}

echo "Starting API on port $PORT..."

# Set Python path to include src directory
export PYTHONPATH=/app/src:$PYTHONPATH

# Start uvicorn
exec uvicorn pinit.api.main:app --host 0.0.0.0 --port $PORT
