# Magic Search AI Enrichment Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fast, asynchronous xAI web-search enrichment layer for Magic Search that is cached by canonical intent and location bucket, while keeping the standard Google/Pinit Magic Search response fast and authoritative.

**Architecture:** Magic Search continues to run the normal Google/Pinit ranking path as the primary response. In parallel, the API reads a Redis area-enrichment cache keyed by canonical intent, radius bucket, freshness, and location bucket; cached enrichment is returned directly to Flutter when available. On cache miss or stale cache, the API enqueues a background job that runs xAI web-search discovery, resolves candidates through Google Places Text Search, matches resolved Google Place IDs to Supabase locations, and writes a Flutter-ready cache payload with a 24-hour TTL.

**Tech Stack:** FastAPI, asyncio, Redis via `ProximalCacheService`, xAI via OpenAI-compatible client, Google Places Text Search, Supabase REST helpers, pytest.

---

## Key Decisions

- xAI must not be asked to produce Google Place IDs.
- xAI must return `place_resolution_query`; Google Places Text Search is the only source of newly resolved `google_place_id` values.
- Cached AI enrichment is returned separately from `recommendations`; it does not replace the standard ranked Magic Search results.
- Redis payloads are Flutter-ready: no additional backend hydration should be required for display.
- First implementation uses deterministic canonical intent signatures from the current `MagicIntent` and prompt terms; no embedding similarity or database tables yet.
- TTL for xAI area enrichment is 24 hours.
- Existing raw-prompt AI cache helpers are replaced by canonical area-enrichment helpers.

## File Structure

- Modify `src/pinit/api/schemas_magic.py`
  - Add `MagicAISearchSignature`, `MagicAIEnrichmentItem`, and `MagicAIEnrichmentPayload` response models.
  - Add debug fields for enrichment cache hit and refresh enqueue status.

- Modify `src/pinit/api/schemas.py`
  - Add `ai_enrichment: Optional[MagicAIEnrichmentPayload]` to `MagicSearchResponse`.
  - Update `enable_live_web_agent` description to describe AI enrichment instead of a blocking live provider.

- Create `src/pinit/api/services/magic_ai_signature.py`
  - Build canonical signatures from `MagicIntent`, prompt, lat/lng, and radius.
  - Produce geo bucket, radius bucket, freshness, canonical text, and stable hash.

- Modify `src/pinit/api/services/cache_service.py`
  - Add `MAGIC_AI_AREA_TTL = 24 * 60 * 60`.
  - Add `build_magic_ai_area_key(...)`, `get_magic_ai_area_results(...)`, and `set_magic_ai_area_results(...)`.
  - Preserve old methods only if tests still depend on them; route new code through the new helpers.

- Modify `src/pinit/api/services/magic_web_agent.py`
  - Restore xAI web-search Responses API usage for enrichment only.
  - Require `place_resolution_query` in the model schema.
  - Remove `google_place_id` from the xAI request schema and prompt.
  - Keep explicit prompt/response logs.

- Modify `src/pinit/api/services/magic_google_service.py`
  - Update `resolve_magic_web_agent_suggestions(...)` to use `place_resolution_query`.
  - Resolve via Google Text Search and attach the resolved Google place dict plus original xAI suggestion.
  - Count one Google resolve call per attempted unresolved suggestion.

- Create `src/pinit/api/services/magic_ai_enrichment.py`
  - Orchestrate xAI discovery, Google resolution, Supabase matching, and payload normalization.
  - Produce `MagicAIEnrichmentPayload` dictionaries ready for Redis and response serialization.

- Modify `src/pinit/api/routers/proximal.py`
  - Build canonical signature after intent parsing.
  - Read Redis enrichment cache before returning.
  - Include cached `ai_enrichment` in the response.
  - Enqueue refresh job on miss/stale when `enable_live_web_agent=True`.
  - Keep standard Google/Pinit Magic Search as the primary response path.

- Modify tests:
  - `tests/test_magic_web_agent.py`
  - `tests/test_magic_search_infrastructure.py`
  - `tests/test_magic_search_agentic_integration.py`
  - Add `tests/test_magic_ai_signature.py`
  - Add `tests/test_magic_ai_enrichment.py`

---

### Task 1: Canonical AI Search Signature

**Files:**
- Create: `src/pinit/api/services/magic_ai_signature.py`
- Test: `tests/test_magic_ai_signature.py`

- [ ] **Step 1: Write the failing signature tests**

Create `tests/test_magic_ai_signature.py`:

```python
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests import install_optional_dependency_stubs

install_optional_dependency_stubs()

from pinit.api.services.magic_ai_signature import (
    build_magic_ai_signature,
    geo_bucket,
    radius_bucket,
)
from pinit.api.services.magic_intent_parser import parse_magic_intent


class MagicAISignatureTests(unittest.TestCase):
    def test_geo_bucket_rounds_to_neighbourhood_scale(self) -> None:
        self.assertEqual(geo_bucket(51.49414, -0.17383), "51.494:-0.174")

    def test_radius_bucket_groups_close_values(self) -> None:
        self.assertEqual(radius_bucket(1.6), "2km")
        self.assertEqual(radius_bucket(4.2), "5km")

    def test_similar_date_prompts_share_canonical_intent_hash(self) -> None:
        first = build_magic_ai_signature(
            intent=parse_magic_intent("cute date night spot near me"),
            raw_prompt="cute date night spot near me",
            lat=51.49414,
            lng=-0.17383,
            radius_km=2.0,
        )
        second = build_magic_ai_signature(
            intent=parse_magic_intent("romantic dinner nearby"),
            raw_prompt="romantic dinner nearby",
            lat=51.49418,
            lng=-0.17380,
            radius_km=2.1,
        )

        self.assertEqual(first["geo_bucket"], second["geo_bucket"])
        self.assertEqual(first["radius_bucket"], second["radius_bucket"])
        self.assertEqual(first["primary_intent"], "date_spot")
        self.assertEqual(second["primary_intent"], "date_spot")
        self.assertEqual(first["meal"], "dinner")
        self.assertEqual(second["meal"], "dinner")
        self.assertEqual(first["intent_hash"], second["intent_hash"])

    def test_freshness_detects_hot_new_and_viral_terms(self) -> None:
        signature = build_magic_ai_signature(
            intent=parse_magic_intent("hot new viral restaurants in South Kensington"),
            raw_prompt="hot new viral restaurants in South Kensington",
            lat=51.49414,
            lng=-0.17383,
            radius_km=2.0,
        )

        self.assertEqual(signature["freshness"], "viral")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
pytest tests/test_magic_ai_signature.py -q
```

Expected: fail because `pinit.api.services.magic_ai_signature` does not exist.

- [ ] **Step 3: Implement signature helpers**

Create `src/pinit/api/services/magic_ai_signature.py`:

```python
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

from pinit.api.schemas_magic import MagicIntent


DATE_TERMS = {"date", "romantic", "first date", "date night"}
DINNER_TERMS = {"dinner", "supper", "evening"}
BRUNCH_TERMS = {"brunch"}
COFFEE_TERMS = {"coffee", "cafe", "work cafe", "laptop"}
DRINKS_TERMS = {"drinks", "cocktails", "bar", "wine"}
VIRAL_TERMS = {"viral", "tiktok", "instagram", "trending"}
RECENT_TERMS = {"hot", "new", "recent", "latest", "opening", "buzz"}


def _normalise_prompt(raw_prompt: str) -> str:
    return re.sub(r"\s+", " ", raw_prompt.lower()).strip()


def geo_bucket(lat: float, lng: float) -> str:
    return f"{round(lat, 3):.3f}:{round(lng, 3):.3f}"


def radius_bucket(radius_km: float) -> str:
    if radius_km <= 1.0:
        return "1km"
    if radius_km <= 2.5:
        return "2km"
    if radius_km <= 5.0:
        return "5km"
    if radius_km <= 10.0:
        return "10km"
    return "50km"


def _contains_any(text: str, terms: set[str]) -> bool:
    return any(term in text for term in terms)


def _primary_intent(intent: MagicIntent, prompt_text: str) -> str:
    if intent.occasion == "date_night" or _contains_any(prompt_text, DATE_TERMS):
        return "date_spot"
    if intent.occasion == "group":
        return "group_dinner"
    if _contains_any(prompt_text, BRUNCH_TERMS):
        return "brunch"
    if _contains_any(prompt_text, COFFEE_TERMS):
        return "work_cafe" if "work" in prompt_text or "laptop" in prompt_text else "coffee"
    if _contains_any(prompt_text, DRINKS_TERMS):
        return "drinks"
    if "hidden" in prompt_text:
        return "hidden_gem"
    if "cheap" in prompt_text:
        return "cheap_eats"
    if _contains_any(prompt_text, VIRAL_TERMS):
        return "viral_food"
    if "new opening" in prompt_text or "new restaurant" in prompt_text:
        return "new_opening"
    return "food_discovery"


def _meal(intent: MagicIntent, prompt_text: str) -> Optional[str]:
    if _contains_any(prompt_text, BRUNCH_TERMS):
        return "brunch"
    if _contains_any(prompt_text, COFFEE_TERMS):
        return "coffee"
    if _contains_any(prompt_text, DRINKS_TERMS):
        return "drinks"
    if _contains_any(prompt_text, DINNER_TERMS) or intent.occasion in {"date_night", "group"}:
        return "dinner"
    if "lunch" in prompt_text:
        return "lunch"
    if "breakfast" in prompt_text:
        return "breakfast"
    if "dessert" in prompt_text:
        return "dessert"
    return None


def _freshness(prompt_text: str) -> str:
    if _contains_any(prompt_text, VIRAL_TERMS):
        return "viral"
    if "new opening" in prompt_text or "just opened" in prompt_text:
        return "new_opening"
    if _contains_any(prompt_text, RECENT_TERMS):
        return "recent"
    return "evergreen"


def _budget(intent: MagicIntent) -> Optional[str]:
    if intent.budget == "cheap":
        return "low"
    if intent.budget == "moderate":
        return "mid"
    if intent.budget == "expensive":
        return "high"
    return None


def _venue_types(intent: MagicIntent, prompt_text: str) -> List[str]:
    if _contains_any(prompt_text, COFFEE_TERMS):
        return ["cafe"]
    if _contains_any(prompt_text, DRINKS_TERMS):
        return ["bar"]
    return list(intent.included_types or ["restaurant"])


def build_magic_ai_signature(
    *,
    intent: MagicIntent,
    raw_prompt: str,
    lat: float,
    lng: float,
    radius_km: float,
) -> Dict[str, Any]:
    prompt_text = _normalise_prompt(raw_prompt)
    canonical = {
        "primary_intent": _primary_intent(intent, prompt_text),
        "meal": _meal(intent, prompt_text),
        "cuisines": sorted(set(intent.cuisines)),
        "vibes": sorted(set(intent.vibe_tags)),
        "venue_types": sorted(set(_venue_types(intent, prompt_text))),
        "budget": _budget(intent),
        "freshness": _freshness(prompt_text),
    }
    canonical_text = " ".join(
        str(part)
        for part in [
            canonical["primary_intent"],
            canonical["meal"] or "any",
            " ".join(canonical["cuisines"]),
            " ".join(canonical["vibes"]),
            " ".join(canonical["venue_types"]),
            canonical["budget"] or "any",
            canonical["freshness"],
        ]
        if part
    )
    hash_payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return {
        **canonical,
        "geo_bucket": geo_bucket(lat, lng),
        "radius_bucket": radius_bucket(radius_km),
        "canonical_text": canonical_text,
        "intent_hash": hashlib.sha1(hash_payload.encode("utf-8")).hexdigest()[:16],
    }
```

- [ ] **Step 4: Verify the signature tests pass**

Run:

```bash
pytest tests/test_magic_ai_signature.py -q
```

Expected: `4 passed`.

---

### Task 2: Redis Area Enrichment Cache

**Files:**
- Modify: `src/pinit/api/services/cache_service.py`
- Test: `tests/test_magic_search_infrastructure.py`

- [ ] **Step 1: Write the failing cache helper test**

Add to `MagicCacheServiceTests` or the existing cache test class in `tests/test_magic_search_infrastructure.py`:

```python
def test_magic_ai_area_cache_key_uses_signature_not_raw_prompt(self) -> None:
    cache = ProximalCacheService(config=_disabled_cache_config())
    signature = {
        "geo_bucket": "51.494:-0.174",
        "radius_bucket": "2km",
        "intent_hash": "abc123",
        "freshness": "recent",
    }

    key = cache.build_magic_ai_area_key(signature)

    self.assertEqual(
        key,
        "magic:ai_area:v2:51.494:-0.174:2km:abc123:recent",
    )
```

- [ ] **Step 2: Run the failing cache test**

Run:

```bash
pytest tests/test_magic_search_infrastructure.py::MagicCacheServiceTests::test_magic_ai_area_cache_key_uses_signature_not_raw_prompt -q
```

Expected: fail because `build_magic_ai_area_key` does not exist.

- [ ] **Step 3: Implement Redis helpers**

In `src/pinit/api/services/cache_service.py`, add near the Magic TTL constants:

```python
MAGIC_AI_AREA_TTL = 24 * 60 * 60
```

Add methods near existing Magic cache helpers:

```python
def build_magic_ai_area_key(self, signature: Dict[str, Any]) -> str:
    return (
        "magic:ai_area:v2:"
        f"{signature['geo_bucket']}:"
        f"{signature['radius_bucket']}:"
        f"{signature['intent_hash']}:"
        f"{signature['freshness']}"
    )

def get_magic_ai_area_results(self, key: str) -> Optional[Dict[str, Any]]:
    return self._magic_cache_get(key)

def set_magic_ai_area_results(
    self,
    key: str,
    payload: Dict[str, Any],
    ttl_seconds: Optional[int] = None,
) -> bool:
    return self._magic_cache_set(
        key,
        payload,
        ttl_seconds or self.MAGIC_AI_AREA_TTL,
    )
```

- [ ] **Step 4: Verify cache helper tests pass**

Run:

```bash
pytest tests/test_magic_search_infrastructure.py -q
```

Expected: all tests in file pass.

---

### Task 3: xAI Web-Search Discovery Schema Without Place IDs

**Files:**
- Modify: `src/pinit/api/services/magic_web_agent.py`
- Test: `tests/test_magic_web_agent.py`

- [ ] **Step 1: Write failing tests for required `place_resolution_query` and no `google_place_id` request**

Update `tests/test_magic_web_agent.py`:

```python
def test_prompt_requires_resolution_query_and_does_not_request_google_place_ids(self) -> None:
    prompt = build_magic_web_agent_prompt(
        user_prompt="hot new restaurants",
        lat=51.4941,
        lng=-0.1738,
        radius_km=2.0,
        vibe_summary={},
        max_candidates=8,
    )

    self.assertIn("place_resolution_query", prompt)
    self.assertIn("Do not invent Google Place IDs", prompt)
    self.assertNotIn("include google_place_id", prompt)

def test_xai_web_search_response_requires_place_resolution_query(self) -> None:
    from pinit.api.services import magic_web_agent

    class _Response:
        output_text = (
            '{"suggestions":[{"name":"Agent Place",'
            '"place_resolution_query":"Agent Place South Kensington London",'
            '"address_hint":"South Kensington","neighbourhood_hint":"South Kensington",'
            '"reason":"Recent mention","confidence":0.8,'
            '"source_claims":["ai_hotspot"],"citations":[]}]}'
        )

    fake_client = AsyncMock()
    fake_client.responses.create = AsyncMock(return_value=_Response())

    with patch.dict("os.environ", {"XAI_API_KEY": ""}), \
         patch.object(magic_web_agent, "XAI_API_KEY", "xai-test-key"), \
         patch.object(magic_web_agent, "AsyncOpenAI", return_value=fake_client):
        result = asyncio_run(
            magic_web_agent.fetch_magic_web_agent_suggestions(
                prompt="hot restaurants nearby",
                lat=51.4941,
                lng=-0.1738,
                radius_km=2.0,
                user_profile={"vibe_vector": []},
                vibe_tag_order={},
                cache=None,
            )
        )

    create_kwargs = fake_client.responses.create.call_args.kwargs
    self.assertEqual(create_kwargs["tools"], [{"type": "web_search"}])
    self.assertNotIn("google_place_id", create_kwargs["input"])
    self.assertEqual(
        result.suggestions[0]["place_resolution_query"],
        "Agent Place South Kensington London",
    )
    self.assertNotIn("google_place_id", result.suggestions[0])
```

- [ ] **Step 2: Run failing xAI tests**

Run:

```bash
pytest tests/test_magic_web_agent.py::MagicWebAgentTests::test_prompt_requires_resolution_query_and_does_not_request_google_place_ids tests/test_magic_web_agent.py::MagicWebAgentTests::test_xai_web_search_response_requires_place_resolution_query -q
```

Expected: fail because current code uses chat completion and does not require `place_resolution_query`.

- [ ] **Step 3: Update `MagicWebAgentSuggestion` and prompt**

In `src/pinit/api/services/magic_web_agent.py`, change the suggestion model:

```python
class MagicWebAgentSuggestion(BaseModel):
    name: str
    place_resolution_query: str
    address_hint: Optional[str] = None
    neighbourhood_hint: Optional[str] = None
    reason: str
    confidence: float = 0.0
    source_claims: List[str] = Field(default_factory=list)
    citations: List[Dict[str, Any]] = Field(default_factory=list)
```

Replace prompt text with:

```python
return (
    "You are helping Pinit find real, currently relevant food/drink venues. "
    "Use live web search for social buzz, critic mentions, new openings, date spot mentions, and local recommendation lists. "
    "Do not invent Google Place IDs. Do not return google_place_id. "
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
```

- [ ] **Step 4: Restore xAI Responses API with web-search tool**

In `fetch_magic_web_agent_suggestions`, replace chat completion call with:

```python
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
```

Update `_response_schema()` so each item requires:

```python
"place_resolution_query": {"type": "string"},
"neighbourhood_hint": {"type": ["string", "null"]},
```

and removes `google_place_id` entirely.

Update `_normalise_web_agent_suggestion()`:

```python
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
```

- [ ] **Step 5: Verify xAI tests pass**

Run:

```bash
pytest tests/test_magic_web_agent.py -q
```

Expected: all web-agent tests pass after updating old chat-completion assertions to Responses API assertions.

---

### Task 4: Google Place ID Resolution From `place_resolution_query`

**Files:**
- Modify: `src/pinit/api/services/magic_google_service.py`
- Test: `tests/test_magic_web_agent.py`

- [ ] **Step 1: Write failing resolver test**

Update `MagicWebAgentResolverTests` in `tests/test_magic_web_agent.py`:

```python
async def test_resolver_uses_place_resolution_query_not_google_place_id_from_xai(self) -> None:
    suggestions = [
        {
            "name": "Test Place",
            "place_resolution_query": "Test Place South Kensington London",
            "address_hint": "South Kensington",
            "neighbourhood_hint": "South Kensington",
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
                    "location": {"latitude": 51.494, "longitude": -0.174},
                }
            ]
        ]
    )

    places = await resolve_magic_web_agent_suggestions(
        suggestions,
        known_google_place_ids=set(),
        lat=51.4941,
        lng=-0.1738,
        radius_km=2.0,
        api_key="test-key",
        client=client,
    )

    self.assertEqual(places[0]["id"], "gid-1")
    self.assertEqual(
        client.calls[0]["textQuery"],
        "Test Place South Kensington London",
    )
    self.assertEqual(places[0]["web_agent"]["place_resolution_query"], "Test Place South Kensington London")
```

- [ ] **Step 2: Run failing resolver test**

Run:

```bash
pytest tests/test_magic_web_agent.py::MagicWebAgentResolverTests::test_resolver_uses_place_resolution_query_not_google_place_id_from_xai -q
```

Expected: fail because resolver currently builds query from `name + address_hint`.

- [ ] **Step 3: Update resolver**

In `resolve_magic_web_agent_suggestions`, replace query construction with:

```python
query = str(suggestion.get("place_resolution_query") or "").strip()
if not query:
    name = str(suggestion.get("name") or "").strip()
    address_hint = str(suggestion.get("address_hint") or "").strip()
    query = f"{name} {address_hint}".strip()
if not query:
    return None
```

Keep `known_google_place_ids` behavior for already resolved cache entries, but do not expect new xAI suggestions to contain `google_place_id`.

- [ ] **Step 4: Verify resolver tests pass**

Run:

```bash
pytest tests/test_magic_web_agent.py -q
```

Expected: all tests pass.

---

### Task 5: AI Enrichment Orchestrator Produces Flutter-Ready Payload

**Files:**
- Create: `src/pinit/api/services/magic_ai_enrichment.py`
- Test: `tests/test_magic_ai_enrichment.py`

- [ ] **Step 1: Write failing orchestrator test**

Create `tests/test_magic_ai_enrichment.py`:

```python
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests import install_optional_dependency_stubs

install_optional_dependency_stubs()

from pinit.api.services.magic_ai_enrichment import build_magic_ai_enrichment_payload


class _FakeSupabase:
    vibe_tag_order = {}

    def get_locations_by_google_place_ids(self, ids):
        if "gid-1" in ids:
            return {"gid-1": {"location_id": 123}}
        return {}


class MagicAIEnrichmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_payload_is_flutter_ready_and_matched_to_supabase(self) -> None:
        signature = {
            "geo_bucket": "51.494:-0.174",
            "radius_bucket": "2km",
            "intent_hash": "abc123",
            "freshness": "recent",
            "primary_intent": "date_spot",
            "meal": "dinner",
            "cuisines": [],
            "vibes": [],
            "venue_types": ["restaurant"],
            "budget": None,
            "canonical_text": "date_spot dinner restaurant recent",
        }
        xai_result = [
            {
                "name": "Test Place",
                "place_resolution_query": "Test Place South Kensington London",
                "address_hint": "South Kensington",
                "neighbourhood_hint": "South Kensington",
                "reason": "Recent mention",
                "confidence": 0.8,
                "source_claims": ["ai_hotspot"],
                "citations": [{"url": "https://example.com", "title": "List"}],
            }
        ]
        google_places = [
            {
                "id": "gid-1",
                "displayName": {"text": "Test Place"},
                "formattedAddress": "South Kensington, London",
                "location": {"latitude": 51.494, "longitude": -0.174},
                "web_agent": xai_result[0],
            }
        ]

        payload = await build_magic_ai_enrichment_payload(
            prompt="hot date spot",
            lat=51.4941,
            lng=-0.1738,
            radius_km=2.0,
            signature=signature,
            user_profile={"vibe_vector": []},
            supabase=_FakeSupabase(),
            fetch_suggestions=AsyncMock(return_value=xai_result),
            resolve_suggestions=AsyncMock(return_value=google_places),
        )

        self.assertEqual(payload["source"], "xai_web_search")
        self.assertEqual(payload["canonical_signature"], signature)
        self.assertEqual(payload["items"][0]["google_place_id"], "gid-1")
        self.assertEqual(payload["items"][0]["matched_location_id"], 123)
        self.assertEqual(payload["items"][0]["match_status"], "supabase_hit")
        self.assertEqual(payload["items"][0]["place_resolution_query"], "Test Place South Kensington London")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run failing orchestrator test**

Run:

```bash
pytest tests/test_magic_ai_enrichment.py -q
```

Expected: fail because `magic_ai_enrichment.py` does not exist.

- [ ] **Step 3: Implement orchestrator**

Create `src/pinit/api/services/magic_ai_enrichment.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional

from pinit.api.services.magic_google_service import resolve_magic_web_agent_suggestions
from pinit.api.services.magic_web_agent import fetch_magic_web_agent_suggestions


FetchSuggestions = Callable[..., Awaitable[Any]]
ResolveSuggestions = Callable[..., Awaitable[List[Dict[str, Any]]]]


def _place_name(place: Dict[str, Any], fallback: str) -> str:
    display = place.get("displayName") or {}
    return str(display.get("text") or fallback)


def _normalise_item(
    *,
    suggestion: Dict[str, Any],
    place: Optional[Dict[str, Any]],
    matched_location_id: Optional[int],
) -> Dict[str, Any]:
    google_place_id = str((place or {}).get("id") or "").strip() or None
    return {
        "google_place_id": google_place_id,
        "name": _place_name(place or {}, str(suggestion.get("name") or "")),
        "place_resolution_query": suggestion.get("place_resolution_query"),
        "address_hint": suggestion.get("address_hint"),
        "neighbourhood_hint": suggestion.get("neighbourhood_hint"),
        "formatted_address": (place or {}).get("formattedAddress"),
        "lat": ((place or {}).get("location") or {}).get("latitude"),
        "lng": ((place or {}).get("location") or {}).get("longitude"),
        "reason": suggestion.get("reason"),
        "confidence": suggestion.get("confidence"),
        "source_claims": list(suggestion.get("source_claims") or []),
        "citations": list(suggestion.get("citations") or []),
        "matched_location_id": matched_location_id,
        "match_status": "supabase_hit" if matched_location_id else ("google_resolved" if google_place_id else "unresolved"),
    }


async def build_magic_ai_enrichment_payload(
    *,
    prompt: str,
    lat: float,
    lng: float,
    radius_km: float,
    signature: Dict[str, Any],
    user_profile: Optional[Dict[str, Any]],
    supabase: Any,
    max_candidates: int = 8,
    fetch_suggestions: FetchSuggestions = fetch_magic_web_agent_suggestions,
    resolve_suggestions: ResolveSuggestions = resolve_magic_web_agent_suggestions,
) -> Dict[str, Any]:
    generated_at = datetime.utcnow()
    result = await fetch_suggestions(
        prompt=prompt,
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        user_profile=user_profile,
        vibe_tag_order=getattr(supabase, "vibe_tag_order", {}),
        cache=None,
        max_candidates=max_candidates,
    )
    suggestions = result.suggestions if hasattr(result, "suggestions") else list(result or [])
    places = await resolve_suggestions(
        suggestions,
        known_google_place_ids=set(),
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        max_google_resolves=max_candidates,
    )

    place_by_query = {
        ((place.get("web_agent") or {}).get("place_resolution_query")): place
        for place in places
    }
    google_ids = [
        str(place.get("id") or "").strip()
        for place in places
        if str(place.get("id") or "").strip()
    ]
    existing_by_gid = supabase.get_locations_by_google_place_ids(google_ids) if google_ids else {}

    items: List[Dict[str, Any]] = []
    for suggestion in suggestions:
        place = place_by_query.get(suggestion.get("place_resolution_query"))
        gid = str((place or {}).get("id") or "").strip()
        location = existing_by_gid.get(gid) if gid else None
        matched_location_id = int(location["location_id"]) if location and location.get("location_id") else None
        items.append(
            _normalise_item(
                suggestion=suggestion,
                place=place,
                matched_location_id=matched_location_id,
            )
        )

    return {
        "source": "xai_web_search",
        "canonical_signature": signature,
        "generated_at": generated_at.isoformat(),
        "expires_at": (generated_at + timedelta(hours=24)).isoformat(),
        "items": items,
    }
```

- [ ] **Step 4: Verify orchestrator tests pass**

Run:

```bash
pytest tests/test_magic_ai_enrichment.py -q
```

Expected: pass.

---

### Task 6: Response Schema For Cached AI Enrichment

**Files:**
- Modify: `src/pinit/api/schemas_magic.py`
- Modify: `src/pinit/api/schemas.py`
- Test: `tests/test_magic_search_agentic_integration.py`

- [ ] **Step 1: Write failing response schema test**

Add to `MagicAgenticSchemaTests` in `tests/test_magic_search_agentic_integration.py`:

```python
def test_response_can_include_cached_ai_enrichment_payload(self) -> None:
    from pinit.api.schemas_magic import MagicAIEnrichmentPayload

    payload = MagicAIEnrichmentPayload(
        source="xai_web_search",
        canonical_signature={"primary_intent": "date_spot"},
        generated_at="2026-05-31T12:00:00",
        expires_at="2026-06-01T12:00:00",
        cache_hit=True,
        items=[
            {
                "google_place_id": "gid-1",
                "name": "Test Place",
                "place_resolution_query": "Test Place Soho London",
                "reason": "Recent mention",
                "confidence": 0.8,
                "source_claims": ["ai_hotspot"],
                "citations": [],
                "matched_location_id": 123,
                "match_status": "supabase_hit",
            }
        ],
    )

    self.assertTrue(payload.cache_hit)
    self.assertEqual(payload.items[0].google_place_id, "gid-1")
```

- [ ] **Step 2: Run failing schema test**

Run:

```bash
pytest tests/test_magic_search_agentic_integration.py::MagicAgenticSchemaTests::test_response_can_include_cached_ai_enrichment_payload -q
```

Expected: fail because models do not exist.

- [ ] **Step 3: Add Pydantic models**

In `src/pinit/api/schemas_magic.py`, add:

```python
class MagicAIEnrichmentItem(BaseModel):
    google_place_id: Optional[str] = None
    name: str
    place_resolution_query: str
    address_hint: Optional[str] = None
    neighbourhood_hint: Optional[str] = None
    formatted_address: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    reason: str
    confidence: float = 0.0
    source_claims: List[str] = Field(default_factory=list)
    citations: List[Dict[str, Any]] = Field(default_factory=list)
    matched_location_id: Optional[int] = None
    match_status: str


class MagicAIEnrichmentPayload(BaseModel):
    source: str
    canonical_signature: Dict[str, Any]
    generated_at: str
    expires_at: str
    cache_hit: bool = False
    refresh_enqueued: bool = False
    items: List[MagicAIEnrichmentItem] = Field(default_factory=list)
```

Add debug fields:

```python
ai_enrichment_cache_hit: bool = False
ai_enrichment_refresh_enqueued: bool = False
ai_enrichment_items: int = 0
```

In `src/pinit/api/schemas.py`, import `MagicAIEnrichmentPayload` and add to `MagicSearchResponse`:

```python
ai_enrichment: Optional[MagicAIEnrichmentPayload] = None
```

- [ ] **Step 4: Verify schema tests pass**

Run:

```bash
pytest tests/test_magic_search_agentic_integration.py::MagicAgenticSchemaTests -q
```

Expected: schema tests pass.

---

### Task 7: Magic Search Route Reads Cache And Enqueues Refresh

**Files:**
- Modify: `src/pinit/api/routers/proximal.py`
- Test: `tests/test_magic_search_agentic_integration.py`

- [ ] **Step 1: Write failing route cache-hit test**

Add to `MagicAgenticMergeTests`:

```python
async def test_magic_search_returns_cached_ai_enrichment_without_blocking_on_refresh(self) -> None:
    from pinit.api.routers import proximal

    request = MagicSearchRequest(
        user_id="user-1",
        latitude=51.5095,
        longitude=-0.1490,
        prompt="hot new date night spots in Soho",
        enable_live_web_agent=True,
    )
    cached_payload = {
        "source": "xai_web_search",
        "canonical_signature": {"primary_intent": "date_spot"},
        "generated_at": "2026-05-31T12:00:00",
        "expires_at": "2026-06-01T12:00:00",
        "cache_hit": True,
        "items": [
            {
                "google_place_id": "gid-1",
                "name": "Test Place",
                "place_resolution_query": "Test Place Soho London",
                "reason": "Recent mention",
                "confidence": 0.8,
                "source_claims": ["ai_hotspot"],
                "citations": [],
                "matched_location_id": 123,
                "match_status": "supabase_hit",
            }
        ],
    }

    with patch.object(proximal, "get_supabase_service") as get_supabase, \
         patch.object(proximal, "get_cache_service") as get_cache, \
         patch.object(proximal, "_get_or_parse_magic_intent") as parse_intent, \
         patch.object(proximal, "_get_magic_user_profile", new=AsyncMock(return_value=({"vibe_vector": []}, False))), \
         patch.object(proximal, "get_or_fetch_google_candidates", new=AsyncMock()) as google, \
         patch.object(proximal, "get_background_job_runner") as get_runner:
        cache = get_cache.return_value
        cache.build_magic_ai_area_key.return_value = "magic:ai_area:v2:test"
        cache.get_magic_ai_area_results.return_value = cached_payload
        supabase = get_supabase.return_value
        supabase.get_locations_by_google_place_ids.return_value = {}
        supabase.get_locations_by_ids.return_value = []
        supabase.vibe_tag_order = {}
        parse_intent.return_value = (proximal.parse_magic_intent(request.prompt), False)
        google.return_value.places = []
        google.return_value.total_google_calls = 0
        google.return_value.cache_hit_google_search = False
        google.return_value.total_candidates_before_dedupe = 0
        google.return_value.total_candidates_after_dedupe = 0
        google.return_value.google_queries = []

        response = await proximal.magic_search(request)

    self.assertTrue(response.ai_enrichment.cache_hit)
    self.assertEqual(response.ai_enrichment.items[0].google_place_id, "gid-1")
    get_runner.return_value.enqueue.assert_not_called()
```

- [ ] **Step 2: Write failing route miss-enqueue test**

Add:

```python
async def test_magic_search_enqueues_ai_enrichment_refresh_on_cache_miss(self) -> None:
    from pinit.api.routers import proximal

    request = MagicSearchRequest(
        user_id="user-1",
        latitude=51.5095,
        longitude=-0.1490,
        prompt="hot new date night spots in Soho",
        enable_live_web_agent=True,
    )

    with patch.object(proximal, "get_supabase_service") as get_supabase, \
         patch.object(proximal, "get_cache_service") as get_cache, \
         patch.object(proximal, "_get_or_parse_magic_intent") as parse_intent, \
         patch.object(proximal, "_get_magic_user_profile", new=AsyncMock(return_value=({"vibe_vector": []}, False))), \
         patch.object(proximal, "get_or_fetch_google_candidates", new=AsyncMock()) as google, \
         patch.object(proximal, "get_background_job_runner") as get_runner:
        cache = get_cache.return_value
        cache.build_magic_ai_area_key.return_value = "magic:ai_area:v2:test"
        cache.get_magic_ai_area_results.return_value = None
        supabase = get_supabase.return_value
        supabase.get_locations_by_google_place_ids.return_value = {}
        supabase.get_locations_by_ids.return_value = []
        supabase.vibe_tag_order = {}
        parse_intent.return_value = (proximal.parse_magic_intent(request.prompt), False)
        google.return_value.places = []
        google.return_value.total_google_calls = 0
        google.return_value.cache_hit_google_search = False
        google.return_value.total_candidates_before_dedupe = 0
        google.return_value.total_candidates_after_dedupe = 0
        google.return_value.google_queries = []

        response = await proximal.magic_search(request)

    self.assertIsNone(response.ai_enrichment)
    self.assertTrue(response.debug.ai_enrichment_refresh_enqueued)
    get_runner.return_value.enqueue.assert_called_once()
```

- [ ] **Step 3: Run failing route tests**

Run:

```bash
pytest tests/test_magic_search_agentic_integration.py::MagicAgenticMergeTests::test_magic_search_returns_cached_ai_enrichment_without_blocking_on_refresh tests/test_magic_search_agentic_integration.py::MagicAgenticMergeTests::test_magic_search_enqueues_ai_enrichment_refresh_on_cache_miss -q
```

Expected: fail because route does not use new cache payload.

- [ ] **Step 4: Implement background refresh helper in route**

In `src/pinit/api/routers/proximal.py`, import:

```python
from pinit.api.schemas_magic import MagicAIEnrichmentPayload
from pinit.api.services.magic_ai_enrichment import build_magic_ai_enrichment_payload
from pinit.api.services.magic_ai_signature import build_magic_ai_signature
```

Add helper:

```python
async def _refresh_magic_ai_enrichment_cache(
    *,
    cache_key: str,
    prompt: str,
    lat: float,
    lng: float,
    radius_km: float,
    signature: Dict[str, Any],
    user_profile: Dict[str, Any],
    supabase: Any,
    cache_service: Any,
) -> None:
    payload = await build_magic_ai_enrichment_payload(
        prompt=prompt,
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        signature=signature,
        user_profile=user_profile,
        supabase=supabase,
    )
    if hasattr(cache_service, "set_magic_ai_area_results"):
        cache_service.set_magic_ai_area_results(cache_key, payload)
```

- [ ] **Step 5: Wire cache lookup and enqueue in `magic_search`**

After intent and user profile are available:

```python
ai_enrichment_payload = None
ai_signature = None
ai_cache_key = None
if request.enable_live_web_agent:
    ai_signature = build_magic_ai_signature(
        intent=intent,
        raw_prompt=request.prompt,
        lat=request.latitude,
        lng=request.longitude,
        radius_km=radius_km,
    )
    ai_cache_key = cache_service.build_magic_ai_area_key(ai_signature)
    cached_ai = cache_service.get_magic_ai_area_results(ai_cache_key)
    if cached_ai is not None:
        cached_ai = dict(cached_ai)
        cached_ai["cache_hit"] = True
        ai_enrichment_payload = MagicAIEnrichmentPayload.model_validate(cached_ai)
        debug.ai_enrichment_cache_hit = True
        debug.ai_enrichment_items = len(ai_enrichment_payload.items)
    else:
        await get_background_job_runner().enqueue(
            "magic_ai_enrichment_refresh",
            _refresh_magic_ai_enrichment_cache,
            cache_key=ai_cache_key,
            prompt=request.prompt,
            lat=request.latitude,
            lng=request.longitude,
            radius_km=radius_km,
            signature=ai_signature,
            user_profile=user_profile,
            supabase=supabase,
            cache_service=cache_service,
        )
        debug.ai_enrichment_refresh_enqueued = True
```

Remove the blocking `fetch_magic_web_agent_suggestions` task from the main route. The AI enrichment path should no longer inject candidates into the primary ranking during this task.

When constructing every `MagicSearchResponse`, pass:

```python
ai_enrichment=ai_enrichment_payload
```

- [ ] **Step 6: Verify route tests pass**

Run:

```bash
pytest tests/test_magic_search_agentic_integration.py -q
```

Expected: pass.

---

### Task 8: Full Verification And Cleanup

**Files:**
- All modified files

- [ ] **Step 1: Run focused Magic Search suite**

Run:

```bash
pytest tests/test_magic_ai_signature.py tests/test_magic_ai_enrichment.py tests/test_magic_web_agent.py tests/test_magic_search_agentic_integration.py tests/test_magic_search_infrastructure.py tests/test_magic_ranking.py tests/test_magic_search_quality_score.py -q
```

Expected: all pass.

- [ ] **Step 2: Run all tests under `tests/`**

Run:

```bash
pytest tests -q
```

Expected: all pass. Existing warnings are acceptable unless a new warning points to changed code.

- [ ] **Step 3: Run root collection check**

Run:

```bash
pytest -q
```

Expected in current repo: this may still fail on the existing unrelated `scripts/test_photo_classification.py` import of missing `GOOGLE_MAPS_API_KEY`. If it fails only for that reason, record it in the final response.

- [ ] **Step 4: Inspect for stale web/chat mismatches**

Run:

```bash
rg "google_place_id.*xAI|Do not browse the web|mode=chat|AI Web|recent web search|responses.create|web_search" src/pinit tests
```

Expected:
- `responses.create` and `web_search` remain only in `magic_web_agent.py` and corresponding tests.
- No prompt asks xAI to produce `google_place_id`.
- No user-facing `AI Web` or `recent web search` labels remain.

- [ ] **Step 5: Final response**

Report:
- xAI web-search enrichment is asynchronous and cached by canonical signature.
- Standard Google/Pinit recommendations remain the primary fast result.
- xAI returns `place_resolution_query`; Google resolves Place IDs; Supabase matching uses those Google IDs.
- Redis payload is Flutter-ready.
- Tests run and any known unrelated failures.

---

## Self-Review

- Spec coverage: The plan covers canonical intent cache keys, Redis area cache, xAI web-search discovery, Google Place ID resolution, Supabase matching, Flutter-ready payloads, non-blocking route behavior, and cache warming.
- Placeholder scan: No `TBD`, `TODO`, or unresolved implementation steps remain.
- Type consistency: `place_resolution_query`, `MagicAIEnrichmentPayload`, `build_magic_ai_signature`, and `build_magic_ai_enrichment_payload` are defined before downstream tasks use them.
- Scope control: Embedding similarity and persistent Supabase cache tables are intentionally excluded from this first implementation to keep the branch testable and fast.
