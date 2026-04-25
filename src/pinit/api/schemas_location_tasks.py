from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


LocationTaskType = Literal[
    "pipeline",
    "details_enrich",
    "emoji",
    "photos",
    "menu_vibe",
    "vibe_reprocess",
]


class LocationTaskPayloadBase(BaseModel):
    task_type: LocationTaskType

    request_id: str
    location_id: int
    google_place_id: str
    source: str

    generate_emoji: bool = True
    classify_photo: bool = True
    created_new: bool = False


class PipelinePayload(LocationTaskPayloadBase):
    task_type: Literal["pipeline"]


class DetailsEnrichPayload(LocationTaskPayloadBase):
    task_type: Literal["details_enrich"]


class EmojiPayload(LocationTaskPayloadBase):
    task_type: Literal["emoji"]


class PhotosPayload(LocationTaskPayloadBase):
    task_type: Literal["photos"]


class MenuVibePayload(LocationTaskPayloadBase):
    task_type: Literal["menu_vibe"]


class VibeReprocessPayload(LocationTaskPayloadBase):
    task_type: Literal["vibe_reprocess"]
    force_blend: bool = False


LocationTaskPayload = Annotated[
    Union[
        PipelinePayload,
        DetailsEnrichPayload,
        EmojiPayload,
        PhotosPayload,
        MenuVibePayload,
        VibeReprocessPayload,
    ],
    Field(discriminator="task_type"),
]

