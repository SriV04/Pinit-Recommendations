from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, TypeAdapter

from pinit.api.schemas_location_tasks import LocationTaskPayload

logger = logging.getLogger(__name__)

_TASK_PAYLOAD_ADAPTER = TypeAdapter(LocationTaskPayload)


class PubSubPushMessage(BaseModel):
    data: str
    attributes: Dict[str, str] = Field(default_factory=dict)
    message_id: Optional[str] = Field(default=None, alias="messageId")
    publish_time: Optional[str] = Field(default=None, alias="publishTime")


class PubSubPushRequest(BaseModel):
    message: PubSubPushMessage
    subscription: Optional[str] = None


def decode_push_payload(push: Dict[str, Any]) -> LocationTaskPayload:
    """Decode a Pub/Sub push request into a LocationTaskPayload."""
    parsed = PubSubPushRequest.model_validate(push)
    raw = base64.b64decode(parsed.message.data).decode("utf-8")
    data = json.loads(raw)
    payload = _TASK_PAYLOAD_ADAPTER.validate_python(data)

    logger.debug(
        "Decoded Pub/Sub push message (message_id=%s subscription=%s task_type=%s location_id=%s request_id=%s)",
        parsed.message.message_id,
        parsed.subscription,
        payload.task_type,
        payload.location_id,
        payload.request_id,
    )

    # Attributes are the source of truth for routing; keep them consistent.
    attr_task_type = parsed.message.attributes.get("task_type")
    if attr_task_type and attr_task_type != payload.task_type:
        raise ValueError(
            f"Pub/Sub attribute task_type={attr_task_type!r} does not match payload.task_type={payload.task_type!r}"
        )

    return payload


@dataclass(frozen=True, slots=True)
class PubSubConfig:
    enabled: bool
    project_id: str
    topic: str


def get_pubsub_config() -> PubSubConfig:
    enabled = os.getenv("PUBSUB_ENABLED", "false").lower() == "true"
    project_id = os.getenv("PUBSUB_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT") or ""
    topic = os.getenv("PUBSUB_TOPIC_LOCATION_TASKS", "")
    return PubSubConfig(enabled=enabled, project_id=project_id, topic=topic)


def _build_attributes(payload: LocationTaskPayload) -> Dict[str, str]:
    return {
        "task_type": payload.task_type,
        "request_id": payload.request_id,
        "location_id": str(payload.location_id),
        "source": payload.source,
    }


def publish_location_task(payload: LocationTaskPayload, *, ordering_key: str) -> str:
    """Publish a task message; returns Pub/Sub message id."""
    cfg = get_pubsub_config()
    if not cfg.enabled:
        raise RuntimeError("PUBSUB_ENABLED is false; cannot publish Pub/Sub tasks")
    if not cfg.project_id:
        raise RuntimeError("PUBSUB_PROJECT_ID/GOOGLE_CLOUD_PROJECT not set")
    if not cfg.topic:
        raise RuntimeError("PUBSUB_TOPIC_LOCATION_TASKS not set")

    try:
        from google.cloud import pubsub_v1
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("google-cloud-pubsub is not installed") from exc

    publisher = pubsub_v1.PublisherClient(
        publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True),
    )
    topic_path = publisher.topic_path(cfg.project_id, cfg.topic)
    body = json.dumps(payload.model_dump(mode="json"), separators=(",", ":")).encode("utf-8")
    attributes = _build_attributes(payload)
    logger.info(
        "Publishing Pub/Sub task %s (location_id=%s request_id=%s ordering_key=%s topic=%s)",
        payload.task_type,
        payload.location_id,
        payload.request_id,
        ordering_key,
        topic_path,
    )
    future = publisher.publish(topic_path, body, ordering_key=ordering_key, **attributes)
    message_id = future.result()
    logger.info(
        "Published Pub/Sub task %s for location %s (message_id=%s)",
        payload.task_type,
        payload.location_id,
        message_id,
    )
    return message_id
