from __future__ import annotations

from typing import Annotated, List, Literal, Tuple, Union

from pydantic import BaseModel, Field


LocationTaskType = Literal[
    "process_location",
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


class ProcessLocationPayload(LocationTaskPayloadBase):
    task_type: Literal["process_location"]


class DetailsEnrichPayload(LocationTaskPayloadBase):
    task_type: Literal["details_enrich"]


class EmojiPayload(LocationTaskPayloadBase):
    task_type: Literal["emoji"]


class PhotosPayload(LocationTaskPayloadBase):
    task_type: Literal["photos"]
    # How many photos to have stored afterwards (primary + extras).
    want: int = Field(3, ge=1, le=10)
    # (name, photoUri) pairs the API already paid for; downloaded instead of
    # making new billed requests.
    photo_uris: List[Tuple[str, str]] = Field(default_factory=list)


class MenuVibePayload(LocationTaskPayloadBase):
    task_type: Literal["menu_vibe"]


class VibeReprocessPayload(LocationTaskPayloadBase):
    task_type: Literal["vibe_reprocess"]
    force_blend: bool = False


LocationTaskPayload = Annotated[
    Union[
        ProcessLocationPayload,
        PipelinePayload,
        DetailsEnrichPayload,
        EmojiPayload,
        PhotosPayload,
        MenuVibePayload,
        VibeReprocessPayload,
    ],
    Field(discriminator="task_type"),
]
