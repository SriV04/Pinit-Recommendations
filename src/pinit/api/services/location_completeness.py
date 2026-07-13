from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


GOOGLE_REFRESH_AFTER = timedelta(days=30)


@dataclass(frozen=True)
class LocationProcessingPlan:
    google_due: bool
    photo_due: bool
    emoji_due: bool
    menu_due: bool
    content_fallback_due: bool
    vibe_due: bool
    reasons: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        return not any(
            (
                self.google_due,
                self.photo_due,
                self.emoji_due,
                self.menu_due,
                self.content_fallback_due,
                self.vibe_due,
            )
        )


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_location_processing_plan(
    row: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> LocationProcessingPlan:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    else:
        current_time = current_time.astimezone(timezone.utc)

    reasons: list[str] = []
    fetched_at = _as_utc(row.get("google_details_fetched_at"))
    if fetched_at is None:
        google_due = True
        reasons.append("google:not_fetched")
    else:
        google_due = current_time - fetched_at >= GOOGLE_REFRESH_AFTER
        if google_due:
            reasons.append("google:stale")

    photo_due = not (
        _truthy(row.get("image_stored"))
        or _truthy(row.get("image_unavailable"))
    )
    if photo_due:
        reasons.append("photo:missing")

    emoji_due = not _present(row.get("emoji"))
    if emoji_due:
        reasons.append("emoji:missing")

    required_content_fields = (
        "generated_summary",
        "cuisine_primary",
        "dietary_requirement_vector",
    )
    missing_content = [
        field for field in required_content_fields if not _present(row.get(field))
    ]
    for field in missing_content:
        reasons.append(f"content:{field}")

    content_fallback_due = bool(missing_content)
    menu_due = content_fallback_due and _present(row.get("website"))

    vibe_due = not (
        _present(row.get("vibe_vector"))
        and _truthy(row.get("updated_vibe"))
    )
    if vibe_due:
        reasons.append("vibe:missing")

    return LocationProcessingPlan(
        google_due=google_due,
        photo_due=photo_due,
        emoji_due=emoji_due,
        menu_due=menu_due,
        content_fallback_due=content_fallback_due,
        vibe_due=vibe_due,
        reasons=tuple(reasons),
    )
