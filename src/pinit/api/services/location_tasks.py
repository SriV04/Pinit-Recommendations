from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import pandas as pd

from pinit.api.schemas_location_tasks import (
    DetailsEnrichPayload,
    EmojiPayload,
    LocationTaskPayload,
    MenuVibePayload,
    PhotosPayload,
    PipelinePayload,
    VibeReprocessPayload,
)
from pinit.api.services.background_jobs import get_background_job_runner
from pinit.api.services.menu_processing import process_menu_for_location
from pinit.api.services.proximal_service import (
    add_location_emoji,
    refresh_location_from_google_place_details,
)
from pinit.api.services.vibe_tagging import generate_vibe_tags_for_location
from pinit.config.secrets import GOOGLE_PLACE_API_KEY
from pinit.integrations.supabase import get_supabase_service

logger = logging.getLogger(__name__)


class LocationTaskDispatcher:
    async def dispatch(self, payload: LocationTaskPayload) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class InProcessDispatcher(LocationTaskDispatcher):
    """Fallback dispatcher for local dev (uses in-process BackgroundJobRunner)."""

    async def dispatch(self, payload: LocationTaskPayload) -> None:
        logger.info(
            "Dispatch (in-process): %s (location_id=%s request_id=%s)",
            payload.task_type,
            payload.location_id,
            payload.request_id,
        )

        async def _handler() -> None:
            await handle_location_task(payload, dispatcher=self)

        await get_background_job_runner().enqueue(
            f"pubsub-fallback:{payload.task_type}:location:{payload.location_id}",
            _handler,
        )


class PubSubDispatcher(LocationTaskDispatcher):
    async def dispatch(self, payload: LocationTaskPayload) -> None:
        from pinit.integrations.pubsub_tasks import publish_location_task

        logger.info(
            "Dispatch (pubsub): %s (location_id=%s request_id=%s)",
            payload.task_type,
            payload.location_id,
            payload.request_id,
        )
        await asyncio.to_thread(
            publish_location_task,
            payload,
            ordering_key=str(payload.location_id),
        )


async def handle_location_task(payload: LocationTaskPayload, *, dispatcher: LocationTaskDispatcher) -> None:
    started = time.monotonic()
    logger.info(
        "Task start: %s (location_id=%s request_id=%s source=%s)",
        payload.task_type,
        payload.location_id,
        payload.request_id,
        payload.source,
    )
    if payload.task_type == "pipeline":
        await pipeline_task(payload, dispatcher=dispatcher)
        logger.info(
            "Task done: %s (location_id=%s request_id=%s duration_ms=%d)",
            payload.task_type,
            payload.location_id,
            payload.request_id,
            int((time.monotonic() - started) * 1000),
        )
        return
    if payload.task_type == "details_enrich":
        await details_enrich_task(payload, dispatcher=dispatcher)
        logger.info(
            "Task done: %s (location_id=%s request_id=%s duration_ms=%d)",
            payload.task_type,
            payload.location_id,
            payload.request_id,
            int((time.monotonic() - started) * 1000),
        )
        return
    if payload.task_type == "emoji":
        await emoji_task(payload)
        logger.info(
            "Task done: %s (location_id=%s request_id=%s duration_ms=%d)",
            payload.task_type,
            payload.location_id,
            payload.request_id,
            int((time.monotonic() - started) * 1000),
        )
        return
    if payload.task_type == "photos":
        await photos_task(payload)
        logger.info(
            "Task done: %s (location_id=%s request_id=%s duration_ms=%d)",
            payload.task_type,
            payload.location_id,
            payload.request_id,
            int((time.monotonic() - started) * 1000),
        )
        return
    if payload.task_type == "menu_vibe":
        await menu_vibe_task(payload)
        logger.info(
            "Task done: %s (location_id=%s request_id=%s duration_ms=%d)",
            payload.task_type,
            payload.location_id,
            payload.request_id,
            int((time.monotonic() - started) * 1000),
        )
        return
    if payload.task_type == "vibe_reprocess":
        await vibe_reprocess_task(payload)
        logger.info(
            "Task done: %s (location_id=%s request_id=%s duration_ms=%d)",
            payload.task_type,
            payload.location_id,
            payload.request_id,
            int((time.monotonic() - started) * 1000),
        )
        return

    raise ValueError(f"Unknown task_type: {payload.task_type}")


async def _get_location(location_id: int) -> Dict[str, Any]:
    supabase = get_supabase_service()
    row = await asyncio.to_thread(supabase.get_location, location_id)
    return row or {}


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    try:
        if isinstance(value, float) and pd.isna(value):
            return False
    except Exception:
        pass
    return bool(value)


async def pipeline_task(payload: PipelinePayload, *, dispatcher: LocationTaskDispatcher) -> None:
    row = await _get_location(payload.location_id)

    if payload.created_new:
        logger.info(
            "pipeline: dispatch details_enrich (location_id=%s request_id=%s generate_emoji=%s classify_photo=%s)",
            payload.location_id,
            payload.request_id,
            bool(payload.generate_emoji),
            bool(payload.classify_photo),
        )
        await dispatcher.dispatch(
            DetailsEnrichPayload(
                task_type="details_enrich",
                **payload.model_dump(exclude={"task_type"}),
            )
        )
        return

    if (payload.source or "").lower().strip() == "magic-search-open":
        logger.info(
            "pipeline: dispatch details_enrich for magic-search-open (location_id=%s request_id=%s generate_emoji=%s classify_photo=%s)",
            payload.location_id,
            payload.request_id,
            bool(payload.generate_emoji),
            bool(payload.classify_photo),
        )
        await dispatcher.dispatch(
            DetailsEnrichPayload(
                task_type="details_enrich",
                **payload.model_dump(exclude={"task_type"}),
            )
        )
        return

    updated_vibe = _truthy(row.get("updated_vibe"))
    source = (payload.source or "in-app").lower().strip()

    if source in ("tiktok", "instagram"):
        logger.info(
            "pipeline: dispatch vibe_reprocess (force_blend=true) (location_id=%s request_id=%s source=%s)",
            payload.location_id,
            payload.request_id,
            source,
        )
        await dispatcher.dispatch(
            VibeReprocessPayload(
                task_type="vibe_reprocess",
                **payload.model_dump(exclude={"task_type"}),
                force_blend=True,
            )
        )
        return

    if not updated_vibe:
        logger.info(
            "pipeline: dispatch vibe_reprocess (force_blend=false) (location_id=%s request_id=%s)",
            payload.location_id,
            payload.request_id,
        )
        await dispatcher.dispatch(
            VibeReprocessPayload(
                task_type="vibe_reprocess",
                **payload.model_dump(exclude={"task_type"}),
                force_blend=False,
            )
        )
        return

    logger.info(
        "pipeline: location %s already has updated_vibe=True; no further tasks",
        payload.location_id,
    )


async def details_enrich_task(payload: DetailsEnrichPayload, *, dispatcher: LocationTaskDispatcher) -> None:
    place_details = await asyncio.to_thread(
        refresh_location_from_google_place_details,
        payload.location_id,
        payload.google_place_id,
    )
    if not place_details:
        # This is a critical dependency for fan-out tasks; fail so Pub/Sub retries.
        raise RuntimeError(
            f"details_enrich failed for location {payload.location_id} ({payload.google_place_id})"
        )

    if payload.generate_emoji:
        logger.info(
            "details_enrich: dispatch emoji (location_id=%s request_id=%s)",
            payload.location_id,
            payload.request_id,
        )
        await dispatcher.dispatch(
            EmojiPayload(
                task_type="emoji",
                **payload.model_dump(exclude={"task_type"}),
            )
        )

    if payload.classify_photo:
        logger.info(
            "details_enrich: dispatch photos (location_id=%s request_id=%s)",
            payload.location_id,
            payload.request_id,
        )
        await dispatcher.dispatch(
            PhotosPayload(
                task_type="photos",
                **payload.model_dump(exclude={"task_type"}),
            )
        )

    logger.info(
        "details_enrich: dispatch menu_vibe (location_id=%s request_id=%s)",
        payload.location_id,
        payload.request_id,
    )
    await dispatcher.dispatch(
        MenuVibePayload(
            task_type="menu_vibe",
            **payload.model_dump(exclude={"task_type"}),
        )
    )


async def emoji_task(payload: EmojiPayload) -> None:
    row = await _get_location(payload.location_id)
    if _truthy(row.get("emoji")):
        logger.info("emoji: skip (already set) (location_id=%s request_id=%s)", payload.location_id, payload.request_id)
        return

    place_details: Dict[str, Any] = {
        "name": row.get("name"),
        "types": row.get("types") or [],
        "price_level": row.get("price_level"),
        "rating": row.get("rating"),
        "editorial_summary": row.get("editorial_summary") or {},
        "reviews": row.get("reviews") or [],
        "vicinity": row.get("vicinity") or "",
    }

    emoji = await asyncio.to_thread(add_location_emoji, place_details)
    if not emoji:
        logger.warning(
            "emoji: generation returned empty; skipping update (location_id=%s request_id=%s)",
            payload.location_id,
            payload.request_id,
        )
        return

    def _update() -> None:
        get_supabase_service().update_location(payload.location_id, emoji=emoji)

    await asyncio.to_thread(_update)
    logger.info("emoji: set %r for location %s", emoji, payload.location_id)


async def photos_task(payload: PhotosPayload) -> None:
    row = await _get_location(payload.location_id)
    if _truthy(row.get("image_stored")) or _truthy(row.get("image_unavailable")):
        logger.info(
            "photos: skip (image already stored/unavailable) (location_id=%s request_id=%s)",
            payload.location_id,
            payload.request_id,
        )
        return

    photos_from_details: List[Dict[str, Any]] = row.get("photos") or []
    await _run_photo_pipeline(payload.location_id, photos_from_details)


async def menu_vibe_task(payload: MenuVibePayload) -> None:
    row = await _get_location(payload.location_id)
    website = row.get("website")

    if website:
        # Idempotency guard: Pub/Sub may retry (5xx) and we don't want to re-run the crawler/LLM
        # menu analysis if it already succeeded once.
        menu_analysis_confidence = row.get("menu_analysis_confidence")
        dietary_vector = row.get("dietary_requirement_vector")
        has_menu_outputs = bool(row.get("menu") or row.get("generated_summary"))
        has_dietary_vector = isinstance(dietary_vector, list) and len(dietary_vector) > 0
        has_prior_menu_analysis = bool(menu_analysis_confidence) and (has_menu_outputs or has_dietary_vector)

        cuisine_value = row.get("cuisine_primary", "") or ""
        try:
            if has_prior_menu_analysis:
                logger.info(
                    "menu_vibe: skip menu analysis (already populated) (location_id=%s request_id=%s confidence=%r)",
                    payload.location_id,
                    payload.request_id,
                    menu_analysis_confidence,
                )
                has_summary = bool(row.get("generated_summary"))
            else:
                menu_result = await process_menu_for_location(
                    location_id=payload.location_id,
                    google_place_id=payload.google_place_id,
                    website=website,
                    restaurant_name=row.get("name") or "",
                    cuisine_hint=cuisine_value,
                    detect_cuisine=not bool(cuisine_value),
                )
                has_summary = bool(menu_result and menu_result.restaurant_description)
        except Exception as exc:
            logger.error("menu_vibe: menu processing failed for %s: %s", payload.location_id, exc)
            has_summary = False
    else:
        has_summary = False

    updated_after_menu = await _get_location(payload.location_id)
    updated_vibe = _truthy(updated_after_menu.get("updated_vibe"))

    blend_tiktok = (payload.source or "").lower().strip() == "tiktok"
    if updated_vibe and not blend_tiktok:
        logger.info(
            "menu_vibe: skip vibe generation (updated_vibe already true) (location_id=%s request_id=%s)",
            payload.location_id,
            payload.request_id,
        )
        return

    if not has_summary:
        has_summary = _truthy(updated_after_menu.get("generated_summary"))
    num_runs = 5 if has_summary else 1

    vibe_vector = await generate_vibe_tags_for_location(
        payload.location_id,
        num_runs=num_runs,
        blend_tiktok=blend_tiktok,
    )
    if vibe_vector is None:
        # Do not ack silently; Pub/Sub retry helps transient LLM/network failures.
        raise RuntimeError(f"menu_vibe vibe tagging returned None for location {payload.location_id}")


async def vibe_reprocess_task(payload: VibeReprocessPayload) -> None:
    row = await _get_location(payload.location_id)
    updated_vibe = _truthy(row.get("updated_vibe"))

    if updated_vibe and not payload.force_blend:
        logger.info(
            "vibe_reprocess: skip (updated_vibe already true and force_blend=false) (location_id=%s request_id=%s)",
            payload.location_id,
            payload.request_id,
        )
        return

    has_summary = _truthy(row.get("generated_summary"))
    num_runs = 5 if has_summary else 1

    vibe_vector = await generate_vibe_tags_for_location(
        payload.location_id,
        num_runs=num_runs,
        blend_tiktok=payload.force_blend,
    )
    if vibe_vector is None:
        raise RuntimeError(f"vibe_reprocess vibe tagging returned None for location {payload.location_id}")


async def run_pipeline_inline(
    payload: PipelinePayload,
) -> None:
    """Inline deterministic pipeline used by /locations/add?process_synchronously=true."""
    row = await _get_location(payload.location_id)
    source = (payload.source or "in-app").lower().strip()

    if payload.created_new:
        details = await asyncio.to_thread(
            refresh_location_from_google_place_details,
            payload.location_id,
            payload.google_place_id,
        )
        if not details:
            return

        if payload.generate_emoji:
            await emoji_task(EmojiPayload(task_type="emoji", **payload.model_dump(exclude={"task_type"})))
        if payload.classify_photo:
            await photos_task(PhotosPayload(task_type="photos", **payload.model_dump(exclude={"task_type"})))
        await menu_vibe_task(MenuVibePayload(task_type="menu_vibe", **payload.model_dump(exclude={"task_type"})))
        return

    if source in ("tiktok", "instagram"):
        await vibe_reprocess_task(
            VibeReprocessPayload(
                task_type="vibe_reprocess",
                force_blend=True,
                **payload.model_dump(exclude={"task_type"}),
            )
        )
        return

    updated_vibe = _truthy(row.get("updated_vibe"))
    if not updated_vibe:
        await vibe_reprocess_task(
            VibeReprocessPayload(
                task_type="vibe_reprocess",
                force_blend=False,
                **payload.model_dump(exclude={"task_type"}),
            )
        )


async def _run_photo_pipeline(location_id: int, photos_from_details: List[Dict[str, Any]]) -> None:
    from pinit.api.services.proximal_service import download_photo

    api_key = GOOGLE_PLACE_API_KEY
    if not api_key:
        logger.warning("photos: GOOGLE_PLACE_API_KEY missing; skipping photo pipeline for %s", location_id)
        return

    bg_supabase = get_supabase_service()

    if not photos_from_details:
        logger.warning(
            "photos: no photos for location %s — marking image_unavailable",
            location_id,
        )
        try:
            await asyncio.to_thread(bg_supabase.mark_location_image_unavailable, location_id)
        except Exception as exc:
            logger.error("photos: mark_location_image_unavailable failed for %s: %s", location_id, exc)
        return

    primary = photos_from_details[0]
    primary_name = primary.get("name") if isinstance(primary, dict) else None
    if not primary_name:
        logger.warning(
            "photos: first photo for location %s has no resource name — marking image_unavailable",
            location_id,
        )
        try:
            await asyncio.to_thread(bg_supabase.mark_location_image_unavailable, location_id)
        except Exception as exc:
            logger.error("photos: mark_location_image_unavailable failed for %s: %s", location_id, exc)
        return

    primary_dl = await asyncio.to_thread(download_photo, primary_name, api_key)
    if primary_dl is None:
        logger.error(
            "photos: primary photo download failed for location %s (name=%s)",
            location_id,
            primary_name,
        )
        return

    primary_bytes, primary_ct = primary_dl
    try:
        await asyncio.to_thread(bg_supabase.upload_location_photo, location_id, primary_bytes, primary_ct, None)
    except Exception as exc:
        logger.error("photos: primary photo upload failed for location %s: %s", location_id, exc)
        return

    try:
        await asyncio.to_thread(
            bg_supabase.mark_location_image_uploaded,
            location_id,
            photos_from_details,
            primary_name,
        )
    except Exception as exc:
        logger.error("photos: mark_location_image_uploaded failed for location %s: %s", location_id, exc)
        return

    extras_uploaded = 0
    for idx, extra in enumerate(photos_from_details[1:10], start=1):
        extra_name = extra.get("name") if isinstance(extra, dict) else None
        if not extra_name:
            continue

        extra_dl = await asyncio.to_thread(download_photo, extra_name, api_key)
        if extra_dl is None:
            logger.warning("photos: extra photo %d download failed for location %s", idx, location_id)
            continue

        extra_bytes, extra_ct = extra_dl
        try:
            await asyncio.to_thread(bg_supabase.upload_location_photo, location_id, extra_bytes, extra_ct, idx)
            extras_uploaded += 1
        except Exception as exc:
            logger.error("photos: extra photo %d upload failed for location %s: %s", idx, location_id, exc)

    if extras_uploaded > 0:
        try:
            await asyncio.to_thread(bg_supabase.mark_location_extra_photos_stored, location_id, extras_uploaded)
        except Exception as exc:
            logger.error("photos: mark_location_extra_photos_stored failed for %s: %s", location_id, exc)

    logger.info("photos: pipeline complete for location %s: primary + %d extras", location_id, extras_uploaded)
