from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError

from pinit.config.secrets import XAI_API_KEY

logger = logging.getLogger(__name__)

XAI_BASE_URL = "https://api.x.ai/v1"
DEFAULT_XAI_MAGIC_SEARCH_MODEL = "grok-4-1-fast-non-reasoning"
DEFAULT_XAI_MAGIC_SEARCH_TIMEOUT_SECONDS = 25.0

ALLOWED_SOURCE_CLAIMS = {
    "ai_hotspot",
    "social_buzz",
    "critic_mentioned",
    "new_opening",
    "date_spot",
    "friend_backed_candidate",
}


class MagicWebAgentSuggestion(BaseModel):
    name: str
    place_resolution_query: str
    address_hint: Optional[str] = None
    neighbourhood_hint: Optional[str] = None
    reason: str
    confidence: float = 0.0
    source_claims: List[str] = Field(default_factory=list)
    citations: List[Dict[str, Any]] = Field(default_factory=list)


class MagicWebAgentResponse(BaseModel):
    suggestions: List[MagicWebAgentSuggestion] = Field(default_factory=list)


@dataclass
class MagicWebAgentResult:
    suggestions: List[Dict[str, Any]] = field(default_factory=list)
    cache_hit: bool = False
    timed_out: bool = False
    error: Optional[str] = None


def _build_vibe_summary(
    vibe_vector: Optional[List[int]],
    vibe_tag_order: Mapping[str, int],
    *,
    limit: int = 5,
) -> Dict[str, float]:
    if not vibe_vector:
        return {}

    scored: List[tuple[str, float]] = []
    for tag, index in vibe_tag_order.items():
        if index >= len(vibe_vector):
            continue
        value = max(0.0, min(float(vibe_vector[index] or 0.0) / 100.0, 1.0))
        if value > 0.0:
            scored.append((tag, value))

    scored.sort(key=lambda item: item[1], reverse=True)
    return {tag: round(value, 2) for tag, value in scored[:limit]}


def build_magic_web_agent_prompt(
    *,
    user_prompt: str,
    lat: float,
    lng: float,
    radius_km: float,
    vibe_summary: Mapping[str, float],
    max_candidates: int,
) -> str:
    vibe_text = ", ".join(
        f"{key}={value:.2f}" for key, value in vibe_summary.items()
    ) or "none"
    return (
        "You are helping Pinit find real, currently relevant food/drink venues. "
        "Use live web search for social buzz, critic mentions, new openings, date spot mentions, and local recommendation lists. "
        "Do not invent Google Place IDs. Return no Google ID fields. "
        "Always provide a place_resolution_query that can be passed to Google Places Text Search. "
        "The place_resolution_query should include venue name plus neighbourhood, address, or city where possible. "
        "Prioritise venues inside or very close to the radius. "
        "Prefer specific real venues, not generic areas or chains unless the branch is clear. "
        f"User request: {user_prompt}\n"
        f"Search center: lat={lat:.5f}, lng={lng:.5f}, radius_km={radius_km:.1f}\n"
        f"User vibe summary: {vibe_text}\n"
        f"Maximum candidates: {max_candidates}\n"
        "Return JSON only. For each candidate include name, place_resolution_query, address_hint, neighbourhood_hint, reason, confidence, source_claims, and citations."
    )


def _normalise_web_agent_suggestion(
    suggestion: MagicWebAgentSuggestion,
) -> Dict[str, Any]:
    claims = [
        claim
        for claim in suggestion.source_claims
        if claim in ALLOWED_SOURCE_CLAIMS
    ]
    return {
        "name": suggestion.name.strip(),
        "place_resolution_query": suggestion.place_resolution_query.strip(),
        "address_hint": (suggestion.address_hint or "").strip() or None,
        "neighbourhood_hint": (suggestion.neighbourhood_hint or "").strip() or None,
        "reason": suggestion.reason.strip(),
        "confidence": max(0.0, min(float(suggestion.confidence), 1.0)),
        "source_claims": claims,
        "citations": suggestion.citations[:3],
    }


def _response_schema() -> Dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "magic_web_agent_response",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "suggestions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "place_resolution_query": {"type": "string"},
                            "address_hint": {"type": ["string", "null"]},
                            "neighbourhood_hint": {"type": ["string", "null"]},
                            "reason": {"type": "string"},
                            "confidence": {"type": "number"},
                            "source_claims": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "citations": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": True,
                                },
                            },
                        },
                        "required": [
                            "name",
                            "place_resolution_query",
                            "address_hint",
                            "neighbourhood_hint",
                            "reason",
                            "confidence",
                            "source_claims",
                            "citations",
                        ],
                    },
                },
            },
            "required": ["suggestions"],
        },
        "strict": True,
    }


def _resolve_timeout_seconds(timeout_seconds: Optional[float]) -> float:
    if timeout_seconds is not None:
        return timeout_seconds

    raw_timeout = os.getenv("XAI_MAGIC_SEARCH_TIMEOUT_SECONDS", "").strip()
    if not raw_timeout:
        return DEFAULT_XAI_MAGIC_SEARCH_TIMEOUT_SECONDS

    try:
        parsed = float(raw_timeout)
    except ValueError:
        logger.warning(
            "Invalid XAI_MAGIC_SEARCH_TIMEOUT_SECONDS=%r; using %.1fs",
            raw_timeout,
            DEFAULT_XAI_MAGIC_SEARCH_TIMEOUT_SECONDS,
        )
        return DEFAULT_XAI_MAGIC_SEARCH_TIMEOUT_SECONDS

    return max(0.001, parsed)


async def fetch_magic_web_agent_suggestions(
    *,
    prompt: str,
    lat: float,
    lng: float,
    radius_km: float,
    user_profile: Optional[Dict[str, Any]],
    vibe_tag_order: Mapping[str, int],
    cache: Any,
    timeout_seconds: Optional[float] = None,
    max_candidates: int = 5,
    client: Optional[Any] = None,
) -> MagicWebAgentResult:
    api_key = os.getenv("XAI_API_KEY", "").strip() or XAI_API_KEY
    if not api_key and client is None:
        logger.warning("XAI_API_KEY is not set; skipping magic web agent")
        return MagicWebAgentResult(error="XAI_API_KEY is not set")

    vibe_summary = _build_vibe_summary(
        (user_profile or {}).get("vibe_vector"),
        vibe_tag_order,
    )

    cache_key = None
    if cache is not None and hasattr(cache, "build_magic_web_agent_key"):
        cache_key = cache.build_magic_web_agent_key(
            prompt=prompt,
            lat=lat,
            lng=lng,
            radius_km=radius_km,
            vibe_summary=vibe_summary,
        )
        cached = cache.get_magic_web_agent_results(cache_key)
        if cached is not None:
            return MagicWebAgentResult(
                suggestions=list(cached.get("suggestions") or []),
                cache_hit=True,
            )

    request_text = build_magic_web_agent_prompt(
        user_prompt=prompt,
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        vibe_summary=vibe_summary,
        max_candidates=max_candidates,
    )

    owns_client = client is None
    if client is None:
        client = AsyncOpenAI(api_key=api_key, base_url=XAI_BASE_URL)

    try:
        resolved_timeout_seconds = _resolve_timeout_seconds(timeout_seconds)
        model = os.getenv(
            "XAI_MAGIC_SEARCH_MODEL",
            DEFAULT_XAI_MAGIC_SEARCH_MODEL,
        )
        logger.info(
            "Grok magic agent prompt (model=%s, mode=responses_web_search):\n%s",
            model,
            request_text,
        )
        response = await asyncio.wait_for(
            client.responses.create(
                model=model,
                input=request_text,
                tools=[{"type": "web_search"}],
                text={"format": _response_schema()},
            ),
            timeout=resolved_timeout_seconds,
        )
        output_text = getattr(response, "output_text", "") or "{}"
        logger.info("Grok magic agent raw response:\n%s", output_text)
        parsed = MagicWebAgentResponse.model_validate(json.loads(output_text))
        suggestions = [
            _normalise_web_agent_suggestion(item)
            for item in parsed.suggestions
            if item.name.strip() and item.reason.strip()
        ]

        if cache_key is not None and cache is not None:
            cache.set_magic_web_agent_results(
                cache_key,
                {
                    "suggestions": suggestions,
                    "cached_at": datetime.utcnow().isoformat(),
                },
            )

        return MagicWebAgentResult(suggestions=suggestions)
    except asyncio.TimeoutError:
        logger.warning(
            "Grok magic agent timed out after %.2fs",
            _resolve_timeout_seconds(timeout_seconds),
        )
        return MagicWebAgentResult(timed_out=True)
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning("Magic web agent returned invalid structured output: %s", exc)
        return MagicWebAgentResult(error="invalid_structured_output")
    except Exception as exc:
        logger.warning("Magic web agent failed: %s", exc)
        return MagicWebAgentResult(error=str(exc))
    finally:
        if owns_client and hasattr(client, "close"):
            await client.close()
