from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from pinit.api.routers import proximal
from pinit.api.services.background_jobs import get_background_job_runner
from pinit.integrations.pubsub_tasks import get_pubsub_config

# Configure logging (respect LOG_LEVEL env)
log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_name, logging.INFO)
logging.basicConfig(
    level=log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Pinit Proximal Recommendations API",
    description="Location-based personalized restaurant recommendations",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(proximal.router)


@app.on_event("startup")
async def startup_background_workers() -> None:
    cfg = get_pubsub_config()
    if cfg.enabled:
        logger.info("Pub/Sub enabled (project_id=%s topic=%s)", cfg.project_id, cfg.topic)
    else:
        logger.warning(
            "Pub/Sub disabled (PUBSUB_ENABLED=false); using in-process background workers. "
            "Set PUBSUB_ENABLED=true and PUBSUB_TOPIC_LOCATION_TASKS=<topic> to enable Pub/Sub."
        )

    # Only start in-process workers when Pub/Sub is disabled (local fallback).
    if not cfg.enabled:
        await get_background_job_runner().start()


@app.on_event("shutdown")
async def shutdown_background_workers() -> None:
    if not get_pubsub_config().enabled:
        await get_background_job_runner().stop()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
