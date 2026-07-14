from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from pinit.api.schemas_location_tasks import ProcessLocationPayload
from pinit.integrations.supabase import SupabaseService

logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 30 * 24 * 60 * 60
CLAIM_STALE_AFTER_SECONDS = 5 * 60

Dispatch = Callable[[ProcessLocationPayload], Awaitable[None]]


@dataclass(frozen=True)
class LocationProcessingAdmissionResult:
    queued: bool
    request_id: str


async def admit_location_processing(
    payload: ProcessLocationPayload,
    *,
    supabase: SupabaseService,
    dispatch: Dispatch,
) -> LocationProcessingAdmissionResult:
    """Atomically admit one location-processing request per cooldown."""
    claimed = await asyncio.to_thread(
        supabase.claim_location_processing,
        payload.location_id,
        payload.request_id,
        cooldown_seconds=COOLDOWN_SECONDS,
        claim_stale_after_seconds=CLAIM_STALE_AFTER_SECONDS,
    )
    if not claimed:
        logger.info(
            "location processing cooldown_skipped "
            "(location_id=%s request_id=%s)",
            payload.location_id,
            payload.request_id,
        )
        return LocationProcessingAdmissionResult(False, payload.request_id)

    logger.info(
        "location processing claim_granted "
        "(location_id=%s request_id=%s)",
        payload.location_id,
        payload.request_id,
    )

    try:
        await dispatch(payload)
    except Exception:
        await asyncio.to_thread(
            supabase.release_location_processing_claim,
            payload.location_id,
            payload.request_id,
        )
        logger.exception(
            "location processing claim_released after dispatch failure "
            "(location_id=%s request_id=%s)",
            payload.location_id,
            payload.request_id,
        )
        raise

    logger.info(
        "location processing publish_succeeded "
        "(location_id=%s request_id=%s)",
        payload.location_id,
        payload.request_id,
    )

    try:
        completed = await asyncio.to_thread(
            supabase.complete_location_processing_queue,
            payload.location_id,
            payload.request_id,
        )
        logger.info(
            "location processing tracker_completed=%s "
            "(location_id=%s request_id=%s)",
            completed,
            payload.location_id,
            payload.request_id,
        )
    except Exception:
        logger.exception(
            "location processing was queued but tracker completion failed; "
            "worker will repair it (location_id=%s request_id=%s)",
            payload.location_id,
            payload.request_id,
        )

    return LocationProcessingAdmissionResult(True, payload.request_id)

