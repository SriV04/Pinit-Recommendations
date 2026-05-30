# Agentic Magic Search Live Web Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an experimental live-web agent layer to Magic Search that discovers current restaurant candidates, validates them through Google Places, merges them with the existing Google/Pinit path, and surfaces source metadata explaining why each result appeared.

**Architecture:** Keep the existing Magic Search endpoint as the stable fast path. Add a parallel OpenAI web-search provider that returns structured candidate hints with citations and reasons, then validate/resolve every hinted place through Google before ranking. Treat web-agent signals as additive metadata and a capped ranking feature so bad or slow web output cannot break the search experience.

**Tech Stack:** FastAPI, Python `asyncio`, OpenAI Responses API with web search and structured output, Google Places API v1 Text Search/Place Details, Redis cache through `ProximalCacheService`, Pydantic v2, pytest/unittest.

---

## References

- OpenAI web search guide: https://platform.openai.com/docs/guides/tools-web-search
- OpenAI structured outputs guide: https://platform.openai.com/docs/guides/structured-outputs
- Existing endpoint: `src/pinit/api/routers/proximal.py`
- Existing Google candidate provider: `src/pinit/api/services/magic_google_service.py`
- Existing ranking layer: `src/pinit/core/recommendation/magic_ranking.py`
- Existing explanation layer: `src/pinit/core/recommendation/magic_explanations.py`
- Existing Magic cache helpers: `src/pinit/api/services/cache_service.py`

## Design Constraints

- The web-agent path is experimental and must be disabled when `OPENAI_API_KEY` is missing.
- Google Places remains the identity authority. A web-agent result is not eligible unless it resolves to a Google Place ID and food-type place.
- First response latency target is unchanged: return results as soon as Google + DB ranking is ready, with the web-agent included only if it finishes within the time budget.
- Initial web-agent timeout: `1.8s`. Initial Google path timeout remains governed by the existing Google service.
- Web-agent ranking influence is capped at `0.08` additive score before final clamping.
- Cache web-agent results by normalized prompt, location cell, radius, and user vibe summary for `30 minutes`.
- Return source metadata in the response for product learning, even when the source has no ranking boost.

## File Structure

- Create `src/pinit/api/services/magic_web_agent.py`
  - Owns OpenAI request construction, structured response validation, citation extraction, cache-key inputs, and conversion to provider-neutral web suggestions.
- Modify `src/pinit/api/services/cache_service.py`
  - Adds web-agent cache helpers and a `v1` key namespace.
- Modify `src/pinit/api/schemas_magic.py`
  - Adds structured web-agent models used by service tests and debug output.
- Modify `src/pinit/api/schemas.py`
  - Adds source metadata fields to `MagicLocationRecommendation` and opt-in request flags.
- Modify `src/pinit/api/routers/proximal.py`
  - Starts the web-agent task in parallel, validates/resolves candidates through Google, merges resolved candidates before reranking, and records debug metrics.
- Modify `src/pinit/core/recommendation/magic_ranking.py`
  - Adds capped `agentic_web_score` support and richer source list handling.
- Modify `src/pinit/core/recommendation/magic_explanations.py`
  - Converts source metadata into deterministic user-facing explanations.
- Test `tests/test_magic_web_agent.py`
  - Unit tests for OpenAI payload construction, structured parsing, cache behavior, and timeout fallback.
- Test `tests/test_magic_search_agentic_integration.py`
  - Endpoint-level tests for merge, validation, ranking, and response metadata.
- Modify `tests/test_magic_search_infrastructure.py`
  - Adds cache helper tests.
- Modify `tests/test_magic_ranking.py`
  - Adds capped web-agent score and source explanation tests.

---

### Task 1: Add Request, Debug, And Source Schemas

**Files:**
- Modify: `src/pinit/api/schemas.py`
- Modify: `src/pinit/api/schemas_magic.py`
- Test: `tests/test_magic_search_agentic_integration.py`

- [ ] **Step 1: Write the failing schema test**

Create `tests/test_magic_search_agentic_integration.py`:

```python
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests import install_optional_dependency_stubs

install_optional_dependency_stubs()

from pinit.api.schemas import MagicLocationRecommendation, MagicSearchRequest
from pinit.api.schemas_magic import MagicSearchDebug


class MagicAgenticSchemaTests(unittest.TestCase):
    def test_request_can_enable_live_web_agent(self) -> None:
        request = MagicSearchRequest(
            user_id="user-1",
            latitude=51.5095,
            longitude=-0.1490,
            prompt="hot new date night spots in Soho",
            enable_live_web_agent=True,
        )

        self.assertTrue(request.enable_live_web_agent)

    def test_recommendation_can_expose_source_metadata(self) -> None:
        item = MagicLocationRecommendation(
            location_id=-1,
            google_place_id="gid-1",
            is_known_location=False,
            name="Test",
            vicinity="Soho",
            lat=51.51,
            lng=-0.13,
            distance_km=0.4,
            vibe_score=0.1,
            dietary_score=0.0,
            quality_score=0.8,
            social_score=0.0,
            collaborative_score=0.0,
            final_score=0.7,
            rank=1,
            source=["Google", "AI Web"],
            source_metadata=[
                {
                    "source": "ai_web",
                    "label": "Mentioned in recent web search",
                    "confidence": 0.74,
                    "url": "https://example.com/list",
                    "detail": "Listed as a current Soho date-night pick",
                }
            ],
        )

        self.assertEqual(item.source_metadata[0]["source"], "ai_web")

    def test_debug_tracks_web_agent_metrics(self) -> None:
        debug = MagicSearchDebug(
            web_agent_latency_ms=123.0,
            web_agent_timed_out=False,
            web_agent_candidates=6,
            web_agent_resolved_candidates=4,
        )

        self.assertEqual(debug.web_agent_candidates, 6)
        self.assertEqual(debug.web_agent_resolved_candidates, 4)
```

- [ ] **Step 2: Run the schema test to verify it fails**

Run:

```bash
pytest tests/test_magic_search_agentic_integration.py::MagicAgenticSchemaTests -q
```

Expected: FAIL because `enable_live_web_agent`, `source_metadata`, and web-agent debug fields do not exist.

- [ ] **Step 3: Add request and response fields**

In `src/pinit/api/schemas.py`, extend `MagicLocationRecommendation`:

```python
class MagicLocationRecommendation(LocationRecommendation):
    source: List[str] = Field(default_factory=list)
    source_metadata: List[Dict[str, Any]] = Field(default_factory=list)
    match_reasons: List[str] = Field(default_factory=list)
    intent_matches: Dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
```

In the same file, extend `MagicSearchRequest`:

```python
class MagicSearchRequest(BaseModel):
    user_id: str = Field(..., description="User identifier")
    latitude: float = Field(..., description="Center point latitude", ge=-90, le=90)
    longitude: float = Field(..., description="Center point longitude", ge=-180, le=180)
    prompt: str = Field(..., description="Free-text search prompt")
    radius_km: Optional[float] = Field(2.0, description="Search radius in kilometers", gt=0, le=50)
    max_results: Optional[int] = Field(20, description="Max number of places to search and rank", ge=1, le=50)
    include_taste_breakdown: Optional[bool] = Field(False, description="Include detailed taste score breakdown")
    enable_live_web_agent: Optional[bool] = Field(
        False,
        description="When true, run the experimental live-web Magic Search provider in parallel.",
    )
```

In `src/pinit/api/schemas_magic.py`, extend `MagicSearchDebug`:

```python
class MagicSearchDebug(BaseModel):
    source: str = "live"
    magic_search_latency_ms: float = 0.0
    intent_parse_latency_ms: float = 0.0
    google_search_latency_ms: float = 0.0
    place_hydration_latency_ms: float = 0.0
    rerank_latency_ms: float = 0.0
    web_agent_latency_ms: float = 0.0
    web_agent_timed_out: bool = False
    web_agent_cache_hit: bool = False
    web_agent_candidates: int = 0
    web_agent_resolved_candidates: int = 0
    cache_hit_google_search: bool = False
    cache_hit_place_details: bool = False
    cache_hit_user_profile: bool = False
    total_google_calls: int = 0
    total_candidates_before_dedupe: int = 0
    total_candidates_after_dedupe: int = 0
    total_ranked: int = 0
    google_queries: List[str] = Field(default_factory=list)
```

- [ ] **Step 4: Run the schema test to verify it passes**

Run:

```bash
pytest tests/test_magic_search_agentic_integration.py::MagicAgenticSchemaTests -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pinit/api/schemas.py src/pinit/api/schemas_magic.py tests/test_magic_search_agentic_integration.py
git commit -m "feat: add magic search agentic metadata schemas"
```

---

### Task 2: Add Web-Agent Cache Helpers

**Files:**
- Modify: `src/pinit/api/services/cache_service.py`
- Test: `tests/test_magic_search_infrastructure.py`

- [ ] **Step 1: Write the failing cache test**

Append to `MagicSearchInfrastructureTests` in `tests/test_magic_search_infrastructure.py`:

```python
    def test_magic_web_agent_cache_helpers_round_trip(self) -> None:
        cache = self._cache()

        key = cache.build_magic_web_agent_key(
            prompt="hot new date night spots in Soho",
            lat=51.5095,
            lng=-0.1490,
            radius_km=2.0,
            vibe_summary={"romantic": 0.91, "trendy": 0.77},
        )
        payload = {
            "suggestions": [{"name": "Test", "google_place_id": "gid-1"}],
            "cached_at": "2026-05-30T12:00:00",
        }

        self.assertTrue(cache.set_magic_web_agent_results(key, payload))
        self.assertEqual(cache.get_magic_web_agent_results(key), payload)
        self.assertTrue(key.startswith("magic:web_agent:v1:"))
```

- [ ] **Step 2: Run the cache test to verify it fails**

Run:

```bash
pytest tests/test_magic_search_infrastructure.py::MagicSearchInfrastructureTests::test_magic_web_agent_cache_helpers_round_trip -q
```

Expected: FAIL because the web-agent cache helper methods do not exist.

- [ ] **Step 3: Add cache helpers**

In `src/pinit/api/services/cache_service.py`, add constants under the Magic Search cache constants:

```python
    MAGIC_WEB_AGENT_TTL = 30 * 60
```

Add these methods near the existing Magic cache helpers:

```python
    def build_magic_web_agent_key(
        self,
        *,
        prompt: str,
        lat: float,
        lng: float,
        radius_km: float,
        vibe_summary: Optional[Dict[str, float]] = None,
    ) -> str:
        lat_cell, lng_cell = self._snap_coordinates(lat, lng)
        prompt_hash = hashlib.sha1(prompt.strip().lower().encode("utf-8")).hexdigest()[:16]
        vibe_payload = json.dumps(vibe_summary or {}, sort_keys=True, separators=(",", ":"))
        vibe_hash = hashlib.sha1(vibe_payload.encode("utf-8")).hexdigest()[:12]
        radius = int(round(radius_km))
        return (
            f"magic:web_agent:v1:{prompt_hash}:g_{lat_cell}_{lng_cell}:"
            f"r{radius}:vibe_{vibe_hash}"
        )

    def get_magic_web_agent_results(self, key: str) -> Optional[Dict[str, Any]]:
        return self._magic_cache_get(key)

    def set_magic_web_agent_results(
        self,
        key: str,
        payload: Dict[str, Any],
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        return self._magic_cache_set(
            key,
            payload,
            ttl_seconds or self.MAGIC_WEB_AGENT_TTL,
        )
```

- [ ] **Step 4: Run the cache test to verify it passes**

Run:

```bash
pytest tests/test_magic_search_infrastructure.py::MagicSearchInfrastructureTests::test_magic_web_agent_cache_helpers_round_trip -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pinit/api/services/cache_service.py tests/test_magic_search_infrastructure.py
git commit -m "feat: cache magic search web agent results"
```

---

### Task 3: Create The OpenAI Live-Web Provider

**Files:**
- Create: `src/pinit/api/services/magic_web_agent.py`
- Modify: `src/pinit/api/schemas_magic.py`
- Test: `tests/test_magic_web_agent.py`

- [ ] **Step 1: Write the failing provider tests**

Create `tests/test_magic_web_agent.py`:

```python
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests import install_optional_dependency_stubs

install_optional_dependency_stubs()

from pinit.api.services.magic_web_agent import (
    MagicWebAgentSuggestion,
    _build_vibe_summary,
    _normalise_web_agent_suggestion,
    build_magic_web_agent_prompt,
)


class MagicWebAgentTests(unittest.TestCase):
    def test_build_vibe_summary_keeps_top_positive_dimensions(self) -> None:
        order = {"romantic": 0, "trendy": 1, "quiet": 2, "cafe": 3}
        summary = _build_vibe_summary([91, 77, 10, 65], order, limit=3)

        self.assertEqual(
            summary,
            {"romantic": 0.91, "trendy": 0.77, "cafe": 0.65},
        )

    def test_prompt_contains_search_context_without_private_vector_dump(self) -> None:
        prompt = build_magic_web_agent_prompt(
            user_prompt="hot new date night spots in Soho",
            lat=51.5095,
            lng=-0.1490,
            radius_km=2.0,
            vibe_summary={"romantic": 0.91, "trendy": 0.77},
            max_candidates=8,
        )

        self.assertIn("hot new date night spots in Soho", prompt)
        self.assertIn("romantic=0.91", prompt)
        self.assertNotIn("[91, 77", prompt)

    def test_normalise_web_agent_suggestion_clamps_confidence(self) -> None:
        suggestion = MagicWebAgentSuggestion(
            name="  Test Place  ",
            google_place_id=" gid-1 ",
            address_hint="Soho",
            reason="Mentioned as a hot date-night spot",
            confidence=2.0,
            source_claims=["ai_hotspot", "social_buzz", "unknown"],
            citations=[{"url": "https://example.com", "title": "List"}],
        )

        normalised = _normalise_web_agent_suggestion(suggestion)

        self.assertEqual(normalised["name"], "Test Place")
        self.assertEqual(normalised["google_place_id"], "gid-1")
        self.assertEqual(normalised["confidence"], 1.0)
        self.assertEqual(normalised["source_claims"], ["ai_hotspot", "social_buzz"])
```

- [ ] **Step 2: Run the provider tests to verify they fail**

Run:

```bash
pytest tests/test_magic_web_agent.py -q
```

Expected: FAIL because `magic_web_agent.py` does not exist.

- [ ] **Step 3: Add web-agent models**

In `src/pinit/api/schemas_magic.py`, add:

```python
class MagicWebAgentCitation(BaseModel):
    url: str
    title: Optional[str] = None


class MagicWebAgentSuggestionModel(BaseModel):
    name: str
    google_place_id: Optional[str] = None
    address_hint: Optional[str] = None
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    source_claims: List[str] = Field(default_factory=list)
    citations: List[MagicWebAgentCitation] = Field(default_factory=list)
```

- [ ] **Step 4: Add the provider module**

Create `src/pinit/api/services/magic_web_agent.py`:

```python
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

logger = logging.getLogger(__name__)

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
    google_place_id: Optional[str] = None
    address_hint: Optional[str] = None
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
    vibe_text = ", ".join(f"{key}={value:.2f}" for key, value in vibe_summary.items()) or "none"
    return (
        "Find current restaurants, cafes, bars, or food venues for a Magic Search request. "
        "Use live web search for recent lists, social buzz, critic mentions, and current hotspot signals. "
        "Return only places that are likely real Google Maps places near the search area. "
        "Do not invent Google Place IDs; omit google_place_id when unsure. "
        f"User request: {user_prompt}\n"
        f"Search center: lat={lat:.5f}, lng={lng:.5f}, radius_km={radius_km:.1f}\n"
        f"User vibe summary: {vibe_text}\n"
        f"Maximum candidates: {max_candidates}\n"
        "For each candidate include name, optional google_place_id, address_hint, reason, confidence, "
        "source_claims, and citations."
    )


def _normalise_web_agent_suggestion(suggestion: MagicWebAgentSuggestion) -> Dict[str, Any]:
    claims = [
        claim
        for claim in suggestion.source_claims
        if claim in ALLOWED_SOURCE_CLAIMS
    ]
    return {
        "name": suggestion.name.strip(),
        "google_place_id": (suggestion.google_place_id or "").strip() or None,
        "address_hint": (suggestion.address_hint or "").strip() or None,
        "reason": suggestion.reason.strip(),
        "confidence": max(0.0, min(float(suggestion.confidence), 1.0)),
        "source_claims": claims,
        "citations": suggestion.citations[:3],
    }


def _response_schema() -> Dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "magic_web_agent_response",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "suggestions": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "google_place_id": {"type": ["string", "null"]},
                            "address_hint": {"type": ["string", "null"]},
                            "reason": {"type": "string"},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "source_claims": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "citations": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "url": {"type": "string"},
                                        "title": {"type": ["string", "null"]},
                                    },
                                    "required": ["url", "title"],
                                },
                            },
                        },
                        "required": [
                            "name",
                            "google_place_id",
                            "address_hint",
                            "reason",
                            "confidence",
                            "source_claims",
                            "citations",
                        ],
                    },
                }
            },
            "required": ["suggestions"],
        },
    }


async def fetch_magic_web_agent_suggestions(
    *,
    prompt: str,
    lat: float,
    lng: float,
    radius_km: float,
    user_profile: Optional[Dict[str, Any]],
    vibe_tag_order: Mapping[str, int],
    cache: Any,
    timeout_seconds: float = 1.8,
    max_candidates: int = 8,
    client: Optional[Any] = None,
) -> MagicWebAgentResult:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key and client is None:
        return MagicWebAgentResult(error="OPENAI_API_KEY is not set")

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
        client = AsyncOpenAI(api_key=api_key)

    try:
        response = await asyncio.wait_for(
            client.responses.create(
                model=os.getenv("OPENAI_MAGIC_SEARCH_MODEL", "gpt-5-mini"),
                input=request_text,
                tools=[{"type": "web_search"}],
                text={"format": _response_schema()},
            ),
            timeout=timeout_seconds,
        )
        output_text = getattr(response, "output_text", "") or "{}"
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
```

- [ ] **Step 5: Run the provider tests to verify they pass**

Run:

```bash
pytest tests/test_magic_web_agent.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pinit/api/services/magic_web_agent.py src/pinit/api/schemas_magic.py tests/test_magic_web_agent.py
git commit -m "feat: add magic search live web agent provider"
```

---

### Task 4: Resolve Web-Agent Hints Through Google Places

**Files:**
- Modify: `src/pinit/api/services/magic_google_service.py`
- Test: `tests/test_magic_web_agent.py`

- [ ] **Step 1: Write the failing resolver test**

Append to `tests/test_magic_web_agent.py`:

```python
from pinit.api.services.magic_google_service import resolve_magic_web_agent_suggestions


class _FakeGoogleClient:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    async def post(self, url, *, json, headers, timeout):
        self.calls.append(json)
        return _FakeGoogleResponse(self.batches.pop(0))


class _FakeGoogleResponse:
    def __init__(self, places):
        self._places = places

    def raise_for_status(self):
        return None

    def json(self):
        return {"places": self._places}


class MagicWebAgentResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolver_searches_name_and_preserves_agent_metadata(self) -> None:
        suggestions = [
            {
                "name": "Test Place",
                "google_place_id": None,
                "address_hint": "Soho",
                "reason": "Recent list mention",
                "confidence": 0.8,
                "source_claims": ["ai_hotspot"],
                "citations": [{"url": "https://example.com", "title": "List"}],
            }
        ]
        client = _FakeGoogleClient(
            [
                [
                    {
                        "id": "gid-1",
                        "displayName": {"text": "Test Place"},
                        "types": ["restaurant"],
                        "location": {"latitude": 51.51, "longitude": -0.13},
                    }
                ]
            ]
        )

        places = await resolve_magic_web_agent_suggestions(
            suggestions,
            lat=51.5095,
            lng=-0.1490,
            radius_km=2.0,
            api_key="test-key",
            client=client,
        )

        self.assertEqual(places[0]["id"], "gid-1")
        self.assertEqual(places[0]["web_agent"]["confidence"], 0.8)
        self.assertIn("Test Place Soho", client.calls[0]["textQuery"])
```

- [ ] **Step 2: Run the resolver test to verify it fails**

Run:

```bash
pytest tests/test_magic_web_agent.py::MagicWebAgentResolverTests::test_resolver_searches_name_and_preserves_agent_metadata -q
```

Expected: FAIL because `resolve_magic_web_agent_suggestions` does not exist.

- [ ] **Step 3: Add the resolver**

In `src/pinit/api/services/magic_google_service.py`, add:

```python
async def resolve_magic_web_agent_suggestions(
    suggestions: List[Dict[str, Any]],
    *,
    lat: float,
    lng: float,
    radius_km: float,
    api_key: Optional[str] = None,
    client: Any = None,
    timeout: float = 2.0,
) -> List[Dict[str, Any]]:
    key = api_key or GOOGLE_PLACE_API_KEY
    if not key or not suggestions:
        return []

    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=timeout)
        close_client = True

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": _FIELD_MASK,
    }
    resolved: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    async def _resolve_one(suggestion: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        name = str(suggestion.get("name") or "").strip()
        if not name:
            return None
        address_hint = str(suggestion.get("address_hint") or "").strip()
        query = f"{name} {address_hint}".strip()
        body: Dict[str, Any] = {
            "textQuery": query,
            "maxResultCount": 3,
            "locationBias": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": float(radius_km * 1000.0),
                }
            },
            "includedType": "restaurant",
        }
        response = await client.post(
            _TEXT_SEARCH_URL,
            json=body,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        places = list(response.json().get("places") or [])
        if not places:
            return None
        place = dict(places[0])
        place["web_agent"] = suggestion
        return place

    try:
        tasks = [_resolve_one(item) for item in suggestions[:8]]
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, Exception) or result is None:
                continue
            place_id = str(result.get("id") or "").strip()
            if not place_id or place_id in seen_ids:
                continue
            seen_ids.add(place_id)
            resolved.append(result)
        return resolved
    finally:
        if close_client:
            await client.aclose()
```

Also import `asyncio` at the top of the file.

- [ ] **Step 4: Run the resolver test to verify it passes**

Run:

```bash
pytest tests/test_magic_web_agent.py::MagicWebAgentResolverTests::test_resolver_searches_name_and_preserves_agent_metadata -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pinit/api/services/magic_google_service.py tests/test_magic_web_agent.py
git commit -m "feat: resolve magic web agent places through google"
```

---

### Task 5: Merge Web-Agent Candidates Into Magic Search

**Files:**
- Modify: `src/pinit/api/routers/proximal.py`
- Test: `tests/test_magic_search_agentic_integration.py`

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_magic_search_agentic_integration.py`:

```python
from unittest.mock import AsyncMock, patch


class MagicAgenticMergeTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_agent_task_is_started_only_when_enabled(self) -> None:
        from pinit.api.routers import proximal

        request = MagicSearchRequest(
            user_id="user-1",
            latitude=51.5095,
            longitude=-0.1490,
            prompt="hot new date night spots in Soho",
            enable_live_web_agent=False,
        )

        with patch.object(proximal, "get_supabase_service") as get_supabase, \
             patch.object(proximal, "get_cache_service") as get_cache, \
             patch.object(proximal, "_get_or_parse_magic_intent") as parse_intent, \
             patch.object(proximal, "_get_magic_user_profile", new=AsyncMock(return_value=({"vibe_vector": []}, False))), \
             patch.object(proximal, "get_or_fetch_google_candidates", new=AsyncMock()) as google, \
             patch.object(proximal, "fetch_magic_web_agent_suggestions", new=AsyncMock()) as web_agent:
            supabase = get_supabase.return_value
            supabase.get_locations_by_google_place_ids.return_value = {}
            supabase.get_locations_by_ids.return_value = []
            supabase.vibe_tag_order = {}
            get_cache.return_value = object()
            parse_intent.return_value = (proximal.parse_magic_intent(request.prompt), False)
            google.return_value.places = []
            google.return_value.total_google_calls = 0
            google.return_value.cache_hit_google_search = False
            google.return_value.total_candidates_before_dedupe = 0
            google.return_value.total_candidates_after_dedupe = 0
            google.return_value.google_queries = []

            await proximal.magic_search(request)

        web_agent.assert_not_awaited()
```

- [ ] **Step 2: Run the integration test to verify it fails**

Run:

```bash
pytest tests/test_magic_search_agentic_integration.py::MagicAgenticMergeTests::test_web_agent_task_is_started_only_when_enabled -q
```

Expected: FAIL because `fetch_magic_web_agent_suggestions` is not imported or integrated.

- [ ] **Step 3: Integrate the web-agent task**

In `src/pinit/api/routers/proximal.py`, update imports:

```python
from pinit.api.services.magic_google_service import (
    get_or_fetch_google_candidates,
    resolve_magic_web_agent_suggestions,
)
from pinit.api.services.magic_web_agent import fetch_magic_web_agent_suggestions
```

After `user_profile` is loaded and before awaiting Google, start the optional web-agent task:

```python
        user_profile, profile_cache_hit = await profile_task
        debug.cache_hit_user_profile = profile_cache_hit
        if not user_profile:
            google_task.cancel()
            raise HTTPException(
                status_code=404,
                detail=f"User '{request.user_id}' not found",
            )

        web_agent_task = None
        if request.enable_live_web_agent:
            web_agent_task = asyncio.create_task(
                fetch_magic_web_agent_suggestions(
                    prompt=request.prompt,
                    lat=request.latitude,
                    lng=request.longitude,
                    radius_km=radius_km,
                    user_profile=user_profile,
                    vibe_tag_order=getattr(supabase, "vibe_tag_order", {}),
                    cache=cache_service,
                )
            )
```

After `google_result = await google_task`, collect web-agent output:

```python
        web_agent_places: List[Dict[str, Any]] = []
        if web_agent_task is not None:
            web_agent_started = perf_counter()
            web_agent_result = await web_agent_task
            debug.web_agent_latency_ms = (perf_counter() - web_agent_started) * 1000.0
            debug.web_agent_timed_out = web_agent_result.timed_out
            debug.web_agent_cache_hit = web_agent_result.cache_hit
            debug.web_agent_candidates = len(web_agent_result.suggestions)
            if web_agent_result.suggestions:
                web_agent_places = await resolve_magic_web_agent_suggestions(
                    web_agent_result.suggestions,
                    lat=request.latitude,
                    lng=request.longitude,
                    radius_km=radius_km,
                )
                debug.web_agent_resolved_candidates = len(web_agent_places)
```

Before assigning `places = google_result.places`, merge:

```python
        places = google_result.places + web_agent_places
```

- [ ] **Step 4: Run the integration test to verify it passes**

Run:

```bash
pytest tests/test_magic_search_agentic_integration.py::MagicAgenticMergeTests::test_web_agent_task_is_started_only_when_enabled -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pinit/api/routers/proximal.py tests/test_magic_search_agentic_integration.py
git commit -m "feat: merge live web candidates into magic search"
```

---

### Task 6: Add Ranking And Explanation Support For Web-Agent Sources

**Files:**
- Modify: `src/pinit/core/recommendation/magic_ranking.py`
- Modify: `src/pinit/core/recommendation/magic_explanations.py`
- Modify: `src/pinit/api/routers/proximal.py`
- Test: `tests/test_magic_ranking.py`

- [ ] **Step 1: Write the failing ranking test**

Append to `tests/test_magic_ranking.py`:

```python
    def test_web_agent_score_is_capped_and_source_metadata_is_preserved(self) -> None:
        from pinit.api.services.magic_intent_parser import parse_magic_intent
        from pinit.core.recommendation.magic_ranking import rerank_magic_candidates

        intent = parse_magic_intent("hot date night in Soho")
        ranked = rerank_magic_candidates(
            [
                {
                    "location_id": -1,
                    "google_place_id": "gid-1",
                    "name": "Test",
                    "lat": 51.51,
                    "lng": -0.13,
                    "distance_km": 0.3,
                    "rating": 4.5,
                    "user_ratings_total": 200,
                    "final_score": 0.4,
                    "web_agent": {
                        "reason": "Recent date-night list mention",
                        "confidence": 1.0,
                        "source_claims": ["ai_hotspot", "date_spot"],
                        "citations": [{"url": "https://example.com", "title": "List"}],
                    },
                }
            ],
            intent=intent,
            request_radius_km=2.0,
        )

        self.assertLessEqual(ranked[0]["intent_matches"]["agentic_web"], 0.08)
        self.assertIn("AI Web", ranked[0]["source"])
        self.assertEqual(ranked[0]["source_metadata"][0]["source"], "ai_web")
        self.assertIn("Mentioned in recent web search", ranked[0]["match_reasons"])
```

- [ ] **Step 2: Run the ranking test to verify it fails**

Run:

```bash
pytest tests/test_magic_ranking.py::MagicRankingTests::test_web_agent_score_is_capped_and_source_metadata_is_preserved -q
```

Expected: FAIL because `agentic_web` and source metadata are not generated.

- [ ] **Step 3: Add source metadata helpers**

In `src/pinit/core/recommendation/magic_ranking.py`, add:

```python
def _agentic_web_score(candidate: Mapping[str, Any]) -> float:
    web_agent = candidate.get("web_agent")
    if not isinstance(web_agent, Mapping):
        return 0.0
    confidence = _clamp(_float(web_agent.get("confidence"), 0.0))
    claims = set(web_agent.get("source_claims") or [])
    claim_bonus = 0.0
    if "ai_hotspot" in claims or "social_buzz" in claims:
        claim_bonus += 0.02
    if "date_spot" in claims or "new_opening" in claims:
        claim_bonus += 0.01
    return min(0.08, confidence * 0.06 + claim_bonus)


def _source_metadata(candidate: Mapping[str, Any]) -> List[Dict[str, Any]]:
    metadata: List[Dict[str, Any]] = []
    web_agent = candidate.get("web_agent")
    if isinstance(web_agent, Mapping):
        citations = list(web_agent.get("citations") or [])
        first_citation = citations[0] if citations else {}
        metadata.append(
            {
                "source": "ai_web",
                "label": "Mentioned in recent web search",
                "confidence": _clamp(_float(web_agent.get("confidence"), 0.0)),
                "url": first_citation.get("url"),
                "title": first_citation.get("title"),
                "detail": web_agent.get("reason"),
                "claims": list(web_agent.get("source_claims") or []),
            }
        )
    return metadata
```

In `_source`, add:

```python
    if isinstance(candidate.get("web_agent"), Mapping):
        source.append("AI Web")
```

In `rerank_magic_candidates`, compute and use the capped score:

```python
        agentic_web = _agentic_web_score(item)
        final_score = (
            0.35 * base_personalised_score
            + 0.25 * intent_scores["overall"]
            + 0.15 * google_quality
            + 0.10 * distance
            + 0.10 * availability
            + 0.05 * novelty
            + agentic_web
        )
```

Add to `score_breakdown`:

```python
            "agentic_web": agentic_web,
```

Set metadata:

```python
        item["source_metadata"] = _source_metadata(item)
```

- [ ] **Step 4: Add a deterministic web-agent explanation**

In `src/pinit/core/recommendation/magic_explanations.py`, add this block before the final fallback reason in `build_match_reasons`:

```python
    if _score(score_breakdown, "agentic_web") >= 0.04:
        _append_unique(reasons, "Mentioned in recent web search")
```

- [ ] **Step 5: Include metadata in API response**

In `src/pinit/api/routers/proximal.py`, when constructing `MagicLocationRecommendation`, add:

```python
                source_metadata=list(candidate.get("source_metadata") or []),
```

- [ ] **Step 6: Run the ranking test to verify it passes**

Run:

```bash
pytest tests/test_magic_ranking.py::MagicRankingTests::test_web_agent_score_is_capped_and_source_metadata_is_preserved -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/pinit/core/recommendation/magic_ranking.py src/pinit/core/recommendation/magic_explanations.py src/pinit/api/routers/proximal.py tests/test_magic_ranking.py
git commit -m "feat: rank magic search web agent signals"
```

---

### Task 7: Latency Guardrails And Fallback Behavior

**Files:**
- Modify: `src/pinit/api/routers/proximal.py`
- Test: `tests/test_magic_search_agentic_integration.py`

- [ ] **Step 1: Write the failing timeout test**

Append to `tests/test_magic_search_agentic_integration.py`:

```python
import asyncio
from pinit.api.services.magic_web_agent import MagicWebAgentResult
```

Then append this method inside `MagicAgenticMergeTests`:

```python
    async def test_web_agent_timeout_does_not_fail_magic_search(self) -> None:
        from pinit.api.routers import proximal

        request = MagicSearchRequest(
            user_id="user-1",
            latitude=51.5095,
            longitude=-0.1490,
            prompt="hot new date night spots in Soho",
            enable_live_web_agent=True,
        )

        async def slow_web_agent(*args, **kwargs):
            await asyncio.sleep(0.01)
            return MagicWebAgentResult(timed_out=True)

        with patch.object(proximal, "get_supabase_service") as get_supabase, \
             patch.object(proximal, "get_cache_service") as get_cache, \
             patch.object(proximal, "_get_or_parse_magic_intent") as parse_intent, \
             patch.object(proximal, "_get_magic_user_profile", new=AsyncMock(return_value=({"vibe_vector": []}, False))), \
             patch.object(proximal, "get_or_fetch_google_candidates", new=AsyncMock()) as google, \
             patch.object(proximal, "fetch_magic_web_agent_suggestions", new=slow_web_agent), \
             patch.object(proximal, "resolve_magic_web_agent_suggestions", new=AsyncMock(return_value=[])):
            supabase = get_supabase.return_value
            supabase.get_locations_by_google_place_ids.return_value = {}
            supabase.get_locations_by_ids.return_value = []
            supabase.vibe_tag_order = {}
            get_cache.return_value = object()
            parse_intent.return_value = (proximal.parse_magic_intent(request.prompt), False)
            google.return_value.places = []
            google.return_value.total_google_calls = 0
            google.return_value.cache_hit_google_search = False
            google.return_value.total_candidates_before_dedupe = 0
            google.return_value.total_candidates_after_dedupe = 0
            google.return_value.google_queries = []
            result = await proximal.magic_search(request)

        self.assertEqual(result.debug.web_agent_resolved_candidates, 0)
```

- [ ] **Step 2: Run the timeout test to verify it fails**

Run:

```bash
pytest tests/test_magic_search_agentic_integration.py::MagicAgenticMergeTests::test_web_agent_timeout_does_not_fail_magic_search -q
```

Expected: FAIL until the router gracefully handles `timed_out=True` without resolution.

- [ ] **Step 3: Add timeout guardrails**

In `src/pinit/api/routers/proximal.py`, wrap web-agent collection:

```python
        web_agent_places: List[Dict[str, Any]] = []
        if web_agent_task is not None:
            web_agent_started = perf_counter()
            web_agent_result = await web_agent_task
            debug.web_agent_latency_ms = (perf_counter() - web_agent_started) * 1000.0
            debug.web_agent_timed_out = web_agent_result.timed_out
            debug.web_agent_cache_hit = web_agent_result.cache_hit
            debug.web_agent_candidates = len(web_agent_result.suggestions)
            if not web_agent_result.timed_out and web_agent_result.suggestions:
                web_agent_places = await resolve_magic_web_agent_suggestions(
                    web_agent_result.suggestions,
                    lat=request.latitude,
                    lng=request.longitude,
                    radius_km=radius_km,
                )
                debug.web_agent_resolved_candidates = len(web_agent_places)
```

- [ ] **Step 4: Run the timeout test to verify it passes**

Run:

```bash
pytest tests/test_magic_search_agentic_integration.py::MagicAgenticMergeTests::test_web_agent_timeout_does_not_fail_magic_search -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pinit/api/routers/proximal.py tests/test_magic_search_agentic_integration.py
git commit -m "fix: keep magic search fast when web agent is slow"
```

---

### Task 8: End-To-End Verification

**Files:**
- Verify only.

- [ ] **Step 1: Run focused unit tests**

Run:

```bash
pytest tests/test_magic_web_agent.py tests/test_magic_search_infrastructure.py tests/test_magic_ranking.py tests/test_magic_search_agentic_integration.py -q
```

Expected: PASS.

- [ ] **Step 2: Run existing Magic Search tests**

Run:

```bash
pytest tests/test_magic_search_quality_score.py tests/test_magic_search_infrastructure.py tests/test_magic_ranking.py -q
```

Expected: PASS.

- [ ] **Step 3: Run a local API smoke test without live web**

Start the API using the project’s existing local command. Then run:

```bash
curl -sS -X POST http://localhost:8000/locations/magic-search \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "demo_date_night",
    "latitude": 51.5095,
    "longitude": -0.1490,
    "prompt": "hot new date night spots in Soho",
    "radius_km": 2,
    "max_results": 10,
    "enable_live_web_agent": false
  }' | python -m json.tool
```

Expected: HTTP 200 with `debug.web_agent_candidates` equal to `0`.

- [ ] **Step 4: Run a local API smoke test with live web**

Ensure `OPENAI_API_KEY` and `GOOGLE_PLACE_API_KEY` are set. Then run:

```bash
curl -sS -X POST http://localhost:8000/locations/magic-search \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "demo_date_night",
    "latitude": 51.5095,
    "longitude": -0.1490,
    "prompt": "hot new date night spots in Soho",
    "radius_km": 2,
    "max_results": 10,
    "enable_live_web_agent": true
  }' | python -m json.tool
```

Expected: HTTP 200. If OpenAI is reachable, `debug.web_agent_candidates` is greater than or equal to `0`, `debug.web_agent_timed_out` accurately reports timeout state, and any web-backed recommendations include `AI Web` in `source`.

- [ ] **Step 5: Commit verification notes if docs changed**

If smoke-test notes are added to docs:

```bash
git add docs/superpowers/plans/2026-05-30-agentic-magic-search-live-web.md
git commit -m "docs: document magic search web agent verification"
```

---

## Launch Criteria

- Web-agent disabled by default.
- When enabled and OpenAI is unavailable, Magic Search still returns Google/Pinit results.
- Web-agent output is cached for repeated prompt/location/vibe searches.
- Every web-agent result is verified through Google Places before being returned.
- Response includes source metadata with citations when web-search output provides them.
- Debug payload makes latency, timeout, cache hit, raw candidate count, and resolved candidate count visible.

## Follow-Up After Experiment

- Compare `web_agent_resolved_candidates / web_agent_candidates` across real requests.
- Compare top-10 click/save rate for `enable_live_web_agent=true` against default Magic Search.
- If live web is useful but slow, move it to a progressive enhancement model: return fast results first, then push/refresh web-backed results in a second response path.
