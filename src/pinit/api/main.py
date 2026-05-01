from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from pinit.api.routers import proximal
from pinit.api.services.background_jobs import get_background_job_runner
from pinit.cli.warm_cache import (
    warm_cache_enabled_from_env,
    warm_cache_end_hour_from_env,
    warm_cache_interval_from_env,
    warm_cache_loop,
    warm_cache_start_hour_from_env,
    warm_cache_timezone_from_env,
    warm_cache_zone_set_from_env,
)
from pinit.integrations.pubsub_tasks import get_pubsub_config

# Configure logging (respect LOG_LEVEL env)
log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_name, logging.INFO)
logging.basicConfig(
    level=log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
_warm_cache_task: asyncio.Task | None = None

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
    global _warm_cache_task
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

    if warm_cache_enabled_from_env() and _warm_cache_task is None:
        _warm_cache_task = asyncio.create_task(
            warm_cache_loop(
                zone_set=warm_cache_zone_set_from_env(),
                interval_seconds=warm_cache_interval_from_env(),
                start_hour=warm_cache_start_hour_from_env(),
                end_hour=warm_cache_end_hour_from_env(),
                timezone_name=warm_cache_timezone_from_env(),
            )
        )
        logger.info(
            "Started warm cache scheduler (zone_set=%s interval=%ss window=%02d:00-%02d:00 timezone=%s)",
            warm_cache_zone_set_from_env(),
            warm_cache_interval_from_env(),
            warm_cache_start_hour_from_env(),
            warm_cache_end_hour_from_env(),
            warm_cache_timezone_from_env(),
        )


@app.on_event("shutdown")
async def shutdown_background_workers() -> None:
    global _warm_cache_task
    if _warm_cache_task is not None:
        _warm_cache_task.cancel()
        with suppress(asyncio.CancelledError):
            await _warm_cache_task
        _warm_cache_task = None
        logger.info("Stopped warm cache scheduler")

    if not get_pubsub_config().enabled:
        await get_background_job_runner().stop()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
