from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request

from pinit.api.services.location_tasks import PubSubDispatcher, handle_location_task
from pinit.integrations.pubsub_tasks import PubSubPushRequest, decode_push_payload

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Pinit Location Task Worker",
    description="Pub/Sub push handler for async location enrichment tasks",
    version="1.0.0",
)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok"}


@app.post("/internal/pubsub/location-tasks")
async def pubsub_location_tasks(request: Request) -> Dict[str, Any]:
    try:
        raw = await request.body()
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    try:
        push = PubSubPushRequest.model_validate(body)
        payload = decode_push_payload(body)
    except Exception as exc:
        # Malformed messages should not be retried forever.
        logger.error("Invalid Pub/Sub payload: %s", exc, exc_info=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    dispatcher = PubSubDispatcher()
    try:
        started = time.monotonic()
        logger.info(
            "Consumed Pub/Sub message (message_id=%s subscription=%s task_type=%s location_id=%s request_id=%s)",
            push.message.message_id,
            push.subscription,
            payload.task_type,
            payload.location_id,
            payload.request_id,
        )
        await handle_location_task(payload, dispatcher=dispatcher)
        logger.info(
            "Ack Pub/Sub message (message_id=%s task_type=%s location_id=%s duration_ms=%d)",
            push.message.message_id,
            payload.task_type,
            payload.location_id,
            int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:
        # Return 5xx so Pub/Sub retries.
        logger.error(
            "Task failed: %s (location_id=%s request_id=%s): %s",
            payload.task_type,
            payload.location_id,
            payload.request_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Task execution failed") from exc

    return {"ok": True}
