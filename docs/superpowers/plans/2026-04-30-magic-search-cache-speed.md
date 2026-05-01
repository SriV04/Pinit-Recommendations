# Magic Search And Cache Speed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce cold magic-search latency by ranking Google Text Search candidates before persistence, and make the proximal Redis cache measurable and warm by default.

**Architecture:** Ship the magic-search latency fix and cache-hit-rate fix as independent tracks. Magic search should call Google Text Search concurrently, build in-memory rankable skeleton candidates, persist only the ranked top results in parallel, and keep the existing `LocationRecommendation.location_id: int` response contract. Cache work should first add warm-up scheduling and endpoint-level hit/miss metrics; Redis GEO and magic-query caching are Phase 2 after the cache is populated and measurable.

**Tech Stack:** FastAPI, asyncio, synchronous `requests` wrapped with `asyncio.to_thread`, Supabase/PostgREST, PostgreSQL migrations, Redis, pytest/unittest.

---

## Assessment Of Claude Plan

Claude correctly identified the primary magic-search bottleneck:

- `src/pinit/api/routers/proximal.py:1764-1780` synchronously fetches full Place Details and inserts every unknown Google place before ranking.
- `src/pinit/api/services/proximal_service.py:880` currently loops over Google included types serially.
- The Text Search payload already includes enough fields to create a rankable cold-start candidate.

The plan needs these corrections before implementation:

- Do not return `pending=True` skeletons in Phase 1. `src/pinit/api/schemas.py` defines `LocationRecommendation.location_id` as a required `int`, and existing clients likely key save/add behavior from that field. Phase 1 should enrich the ranked top-K before response and drop failed enrichments.
- Do not rely on an existing `locations.google_place_id` uniqueness constraint. `migrations/schema.sql` only shows `locations_pkey`; no unique constraint exists for `google_place_id`. Add a migration and change `create_location` to upsert before parallel top-K enrichment.
- Do not put Redis GEO in Phase 1. The current biggest cache miss reason is that no process schedules `warm_cache.py`; metrics are also missing. Warm-up plus metrics should land first so GEO can be validated against real hit/miss and coverage data.
- Keep text-search timeouts realistic. Wrapping a blocking `requests.post(timeout=30)` in `asyncio.wait_for(timeout=8)` does not stop the underlying worker thread. Use a shorter per-call request timeout inside the worker and treat one included-type failure as an empty shard.
- Magic search already has access to `get_or_build_user_profile`; use it during skeleton ranking so user vectors are still applied without extra Supabase reads.

## File Structure

- Modify `src/pinit/api/services/proximal_service.py`
  - Add `compute_google_baseline_in_memory`.
  - Extract one Google Text Search shard function.
  - Add `async text_search_async`.
- Modify `src/pinit/api/routers/proximal.py`
  - Build rankable skeleton candidates from Text Search.
  - Overlay known DB rows.
  - Rank before persistence.
  - Persist ranked top-K unknown places in parallel.
  - Pass cached user profile and zero social/collab weights for magic-search skeleton ranking.
  - Guard social/collab Supabase calls when their effective weights are zero.
- Modify `src/pinit/integrations/supabase.py`
  - Change `create_location` to upsert on `google_place_id` when present.
- Create `migrations/v6_locations_google_place_id_unique.sql`
  - Assert there are no duplicate non-null Google Place IDs.
  - Add a partial unique index on `locations(google_place_id)`.
- Modify `src/pinit/cli/warm_cache.py`
  - Add one reusable warm pass and an async warm loop with Redis lock.
- Modify `src/pinit/api/main.py`
  - Start and cancel the warm-cache loop with the FastAPI process.
- Create `src/pinit/api/services/cache_metrics.py`
  - In-memory endpoint/outcome counters.
- Modify `src/pinit/api/services/cache_service.py`
  - Accept an `endpoint` label in cache reads.
  - Increment cache hit/miss metrics.
  - Log cache configuration at Redis connection.
- Modify `src/pinit/api/schemas.py`
  - Add a small cache stats response model only if the router endpoint wants typed output.
- Modify tests:
  - `tests/test_magic_search_quality_score.py`
  - `tests/test_magic_search_fast_path.py`
  - `tests/test_cache_metrics.py`

## Task 1: Pin The Google Baseline Formula In Python

**Files:**
- Modify: `src/pinit/api/services/proximal_service.py`
- Test: `tests/test_magic_search_quality_score.py`

- [ ] **Step 1: Write the failing formula test**

Append these cases to `MagicSearchQualityScoreTests` in `tests/test_magic_search_quality_score.py`:

```python
    def test_google_baseline_in_memory_matches_sql_formula_samples(self) -> None:
        from pinit.api.services.proximal_service import compute_google_baseline_in_memory

        samples = [
            (None, None, 0.0),
            (4.5, 0, 0.0),
            (3.8, 50, 0.5484811887),
            (4.2, 100, 0.6462045247),
            (4.7, 500, 0.8774545455),
            (5.0, 2000, 0.9932734325),
        ]

        for rating, count, expected in samples:
            with self.subTest(rating=rating, count=count):
                actual = compute_google_baseline_in_memory(rating, count)
                self.assertAlmostEqual(actual, expected, places=9)
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
pytest tests/test_magic_search_quality_score.py::MagicSearchQualityScoreTests::test_google_baseline_in_memory_matches_sql_formula_samples -q
```

Expected: FAIL with `ImportError` or `AttributeError` because `compute_google_baseline_in_memory` does not exist.

- [ ] **Step 3: Add the formula implementation**

Add near the Text Search helpers in `src/pinit/api/services/proximal_service.py`:

```python
def compute_google_baseline_in_memory(
    rating: Optional[float],
    user_ratings_total: Optional[float],
) -> float:
    """Mirror migrations/v4_app_signal_pillars.sql compute_google_baseline_score."""
    review_count = float(user_ratings_total or 0)
    rating_value = float(rating if rating is not None else 3.8)

    bayesian_rating = ((rating_value * review_count) + (3.8 * 50)) / (review_count + 50)
    if review_count <= 0:
        review_trust = 0.0
    else:
        review_trust = min(
            0.95 * ((math.log(1 + review_count) / math.log(501)) ** 0.6),
            1.0,
        )

    anti_mispricing_cap = min(review_trust, 0.85 + 0.15 * (bayesian_rating / 5.0))
    return float((bayesian_rating / 5.0) * anti_mispricing_cap)
```

Ensure `math` and `Optional` are imported in the file.

- [ ] **Step 4: Run the focused test and verify it passes**

Run:

```bash
pytest tests/test_magic_search_quality_score.py::MagicSearchQualityScoreTests::test_google_baseline_in_memory_matches_sql_formula_samples -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pinit/api/services/proximal_service.py tests/test_magic_search_quality_score.py
git commit -m "test: pin in-memory google baseline score"
```

## Task 2: Add Concurrent Google Text Search

**Files:**
- Modify: `src/pinit/api/services/proximal_service.py`
- Test: `tests/test_magic_search_fast_path.py`

- [ ] **Step 1: Write the failing async text-search test**

Create `tests/test_magic_search_fast_path.py`:

```python
import asyncio
import time
import unittest
from unittest.mock import patch

from pinit.api.services import proximal_service


class MagicSearchFastPathTests(unittest.TestCase):
    def test_text_search_async_runs_type_shards_concurrently_and_dedupes(self) -> None:
        calls = []

        def fake_one_type(place_type, base_body, headers, url, request_timeout_s):
            calls.append(place_type)
            time.sleep(0.05)
            return [
                {"id": f"{place_type}-1", "types": [place_type]},
                {"id": "shared", "types": [place_type]},
            ], 1

        started = time.perf_counter()
        with patch.object(proximal_service, "_text_search_one_type", side_effect=fake_one_type):
            places, api_calls = asyncio.run(
                proximal_service.text_search_async(
                    query="date night",
                    place_types=["restaurant", "cafe", "bar"],
                    lat=51.5137,
                    lng=-0.1310,
                    radius_m=2000,
                    per_type_timeout_s=1.0,
                )
            )
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 0.12)
        self.assertEqual(api_calls, 3)
        self.assertEqual({p["id"] for p in places}, {"restaurant-1", "cafe-1", "bar-1", "shared"})
        self.assertEqual(calls, ["restaurant", "cafe", "bar"])
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
pytest tests/test_magic_search_fast_path.py::MagicSearchFastPathTests::test_text_search_async_runs_type_shards_concurrently_and_dedupes -q
```

Expected: FAIL because `text_search_async` and `_text_search_one_type` do not exist.

- [ ] **Step 3: Extract the one-type worker and add async wrapper**

In `src/pinit/api/services/proximal_service.py`, keep `text_search` as the sync compatibility wrapper and move the body of the per-type loop into this helper:

```python
def _text_search_one_type(
    place_type: Optional[str],
    base_body: Dict[str, Any],
    headers: Dict[str, str],
    url: str,
    request_timeout_s: float = 8.0,
) -> Tuple[List[dict], int]:
    body = dict(base_body)
    if place_type is not None:
        body["includedType"] = place_type

    max_retries = 2
    backoff_base = 2.0
    max_wait = 2.0
    sleep_s = 0.15

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=request_timeout_s)
        except requests.RequestException as exc:
            wait = min(sleep_s * backoff_base ** (attempt - 1), max_wait)
            logger.warning(
                "HTTP error on text-search attempt %d/%d (type=%s): %s; retrying in %.1fs",
                attempt,
                max_retries,
                place_type,
                exc,
                wait,
            )
            time.sleep(wait * random.uniform(0.85, 1.15))
            continue

        if resp.status_code == 429:
            wait = min(0.5 * backoff_base ** (attempt - 1), max_wait)
            logger.warning(
                "Rate limited on text-search attempt %d/%d (type=%s); retrying in %.1fs",
                attempt,
                max_retries,
                place_type,
                wait,
            )
            time.sleep(wait * random.uniform(0.85, 1.15))
            continue

        if resp.status_code == 403:
            raise RuntimeError(f"Google Places API 403 Forbidden: {resp.text[:200]}")

        if resp.status_code >= 500:
            wait = min(0.5 * backoff_base ** (attempt - 1), max_wait)
            logger.warning(
                "Server error %d on text-search attempt %d/%d (type=%s); retrying in %.1fs",
                resp.status_code,
                attempt,
                max_retries,
                place_type,
                wait,
            )
            time.sleep(wait * random.uniform(0.85, 1.15))
            continue

        if resp.status_code == 400:
            logger.error("Bad text-search request for type=%s: %s", place_type, resp.text[:300])
            return [], 1

        resp.raise_for_status()
        data = resp.json()
        return list(data.get("places", [])), 1

    logger.warning("text_search shard failed after retries for type=%s", place_type)
    return [], 0
```

Then add:

```python
def _build_text_search_request(
    query: str,
    lat: Optional[float],
    lng: Optional[float],
    radius_m: Optional[float],
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    url = "https://places.googleapis.com/v1/places:searchText"
    field_mask = ",".join([
        "places.id",
        "places.displayName",
        "places.types",
        "places.formattedAddress",
        "places.shortFormattedAddress",
        "places.location",
        "places.businessStatus",
        "places.googleMapsUri",
        "places.photos",
        "places.rating",
        "places.userRatingCount",
        "places.priceLevel",
        "places.currentOpeningHours",
        "places.reviews",
        "places.websiteUri",
        "places.reviewSummary",
        "places.goodForChildren",
        "places.goodForGroups",
        "places.goodForWatchingSports",
        "places.liveMusic",
        "places.outdoorSeating",
        "places.servesBeer",
        "places.servesBreakfast",
        "places.servesBrunch",
        "places.servesCocktails",
        "places.servesCoffee",
        "places.servesDessert",
        "places.servesDinner",
        "places.servesLunch",
        "places.servesVegetarianFood",
        "places.servesWine",
    ])

    api_key = GOOGLE_PLACE_API_KEY
    if not api_key:
        raise ValueError("GOOGLE_PLACE_API_KEY not set")

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": field_mask,
    }
    base_body: Dict[str, Any] = {"textQuery": query, "maxResultCount": 20}
    if lat is not None and lng is not None and radius_m is not None:
        base_body["locationBias"] = {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(radius_m),
            }
        }
    return url, headers, base_body


async def text_search_async(
    query: str,
    place_types: List[str],
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    radius_m: Optional[float] = None,
    per_type_timeout_s: float = 8.0,
) -> Tuple[List[dict], int]:
    url, headers, base_body = _build_text_search_request(query, lat, lng, radius_m)
    types_to_query: List[Optional[str]] = list(place_types) if place_types else [None]

    async def run_one(place_type: Optional[str]) -> Tuple[List[dict], int]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    _text_search_one_type,
                    place_type,
                    base_body,
                    headers,
                    url,
                    per_type_timeout_s,
                ),
                timeout=per_type_timeout_s + 1.0,
            )
        except Exception as exc:
            logger.warning("Text-search shard failed for type=%s: %s", place_type, exc)
            return [], 0

    shard_results = await asyncio.gather(*(run_one(place_type) for place_type in types_to_query))
    seen: Dict[str, dict] = {}
    total_calls = 0
    for places, calls in shard_results:
        total_calls += calls
        for place in places:
            pid = place.get("id")
            if pid and pid not in seen:
                seen[pid] = place
    return list(seen.values()), total_calls
```

Finally, rewrite `text_search` to call the same helpers synchronously:

```python
def text_search(
    query: str,
    place_types: List[str],
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    radius_m: Optional[float] = None,
) -> Tuple[List[dict], int]:
    url, headers, base_body = _build_text_search_request(query, lat, lng, radius_m)
    seen: Dict[str, dict] = {}
    total_calls = 0
    for place_type in (list(place_types) if place_types else [None]):
        places, calls = _text_search_one_type(place_type, base_body, headers, url)
        total_calls += calls
        for place in places:
            pid = place.get("id")
            if pid and pid not in seen:
                seen[pid] = place
    return list(seen.values()), total_calls
```

- [ ] **Step 4: Run the focused test and the existing quality tests**

Run:

```bash
pytest tests/test_magic_search_fast_path.py tests/test_magic_search_quality_score.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pinit/api/services/proximal_service.py tests/test_magic_search_fast_path.py
git commit -m "feat: run magic text search shards concurrently"
```

## Task 3: Make Location Persistence Idempotent Before Parallel Enrichment

**Files:**
- Create: `migrations/v6_locations_google_place_id_unique.sql`
- Modify: `src/pinit/integrations/supabase.py`
- Test: `tests/test_magic_search_fast_path.py`

- [ ] **Step 1: Write a failing Supabase upsert test**

Append to `tests/test_magic_search_fast_path.py`:

```python
class _FakeTable:
    def __init__(self):
        self.insert_called = False
        self.upsert_called = False
        self.payload = None
        self.on_conflict = None

    def upsert(self, payload, on_conflict=None):
        self.upsert_called = True
        self.payload = payload
        self.on_conflict = on_conflict
        return self

    def insert(self, payload):
        self.insert_called = True
        self.payload = payload
        return self

    def execute(self):
        return type("Response", (), {"data": [{"location_id": 123, **self.payload}]})()


class _FakeSupabaseClient:
    def __init__(self):
        self.locations = _FakeTable()

    def table(self, name):
        assert name == "locations"
        return self.locations


class SupabaseLocationWriteTests(unittest.TestCase):
    def test_create_location_upserts_when_google_place_id_is_present(self) -> None:
        from pinit.integrations.supabase import SupabaseService

        service = SupabaseService.__new__(SupabaseService)
        service.client = _FakeSupabaseClient()

        with patch("pinit.integrations.supabase._get_cache_service", return_value=None):
            row = service.create_location(name="Known", google_place_id="gid-1", lat=51.5, lng=-0.1)

        self.assertEqual(row["location_id"], 123)
        self.assertTrue(service.client.locations.upsert_called)
        self.assertFalse(service.client.locations.insert_called)
        self.assertEqual(service.client.locations.on_conflict, "google_place_id")
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
pytest tests/test_magic_search_fast_path.py::SupabaseLocationWriteTests::test_create_location_upserts_when_google_place_id_is_present -q
```

Expected: FAIL because `create_location` still calls `insert`.

- [ ] **Step 3: Add the migration**

Create `migrations/v6_locations_google_place_id_unique.sql`:

```sql
-- Ensure parallel magic-search enrichment cannot insert duplicate Google places.

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM public.locations
    WHERE google_place_id IS NOT NULL
    GROUP BY google_place_id
    HAVING COUNT(*) > 1
  ) THEN
    RAISE EXCEPTION 'Duplicate non-null locations.google_place_id values exist; resolve duplicates before adding the unique index';
  END IF;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS locations_google_place_id_unique_idx
ON public.locations (google_place_id)
WHERE google_place_id IS NOT NULL;
```

- [ ] **Step 4: Change `create_location` to upsert when a Google Place ID exists**

In `src/pinit/integrations/supabase.py`, replace the write line in `create_location` with:

```python
        table = self.client.table("locations")
        if data.get("google_place_id"):
            response = table.upsert(data, on_conflict="google_place_id").execute()
        else:
            response = table.insert(data).execute()
```

Keep the existing geographic cache invalidation block unchanged.

- [ ] **Step 5: Run the focused test**

Run:

```bash
pytest tests/test_magic_search_fast_path.py::SupabaseLocationWriteTests::test_create_location_upserts_when_google_place_id_is_present -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add migrations/v6_locations_google_place_id_unique.sql src/pinit/integrations/supabase.py tests/test_magic_search_fast_path.py
git commit -m "feat: make google place location writes idempotent"
```

## Task 4: Rank Magic Search From Skeletons And Enrich Top-K Only

**Files:**
- Modify: `src/pinit/api/routers/proximal.py`
- Modify: `src/pinit/api/services/proximal_service.py`
- Test: `tests/test_magic_search_fast_path.py`

- [ ] **Step 1: Write skeleton helper tests**

Append to `tests/test_magic_search_fast_path.py`:

```python
class MagicSearchSkeletonTests(unittest.TestCase):
    def test_build_magic_search_skeleton_uses_text_search_payload(self) -> None:
        from pinit.api.routers.proximal import _build_magic_search_skeleton

        place = {
            "id": "gid-1",
            "displayName": {"text": "Fast Noodles"},
            "shortFormattedAddress": "Soho",
            "location": {"latitude": 51.513, "longitude": -0.131},
            "rating": 4.6,
            "userRatingCount": 321,
            "priceLevel": "PRICE_LEVEL_MODERATE",
            "types": ["restaurant", "food"],
            "currentOpeningHours": {"openNow": True},
            "photos": [{"name": "places/gid-1/photos/a"}],
        }

        row = _build_magic_search_skeleton(place)

        self.assertIsNone(row["location_id"])
        self.assertEqual(row["google_place_id"], "gid-1")
        self.assertEqual(row["name"], "Fast Noodles")
        self.assertEqual(row["vicinity"], "Soho")
        self.assertEqual(row["lat"], 51.513)
        self.assertEqual(row["lng"], -0.131)
        self.assertEqual(row["rating"], 4.6)
        self.assertEqual(row["user_ratings_total"], 321)
        self.assertEqual(row["price_level"], 2)
        self.assertFalse(row["has_app_signal"])
        self.assertGreater(row["google_baseline_score"], 0.0)
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
pytest tests/test_magic_search_fast_path.py::MagicSearchSkeletonTests::test_build_magic_search_skeleton_uses_text_search_payload -q
```

Expected: FAIL because `_build_magic_search_skeleton` does not exist.

- [ ] **Step 3: Add skeleton conversion helpers**

In `src/pinit/api/routers/proximal.py`, import `compute_google_baseline_in_memory` and `text_search_async` from `proximal_service`, then add:

```python
_GOOGLE_PRICE_LEVELS = {
    "PRICE_LEVEL_FREE": 0,
    "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2,
    "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}


def _price_level_value(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    return float(_GOOGLE_PRICE_LEVELS[raw]) if raw in _GOOGLE_PRICE_LEVELS else None


def _google_display_name(place: Dict[str, Any]) -> str:
    display = place.get("displayName")
    if isinstance(display, dict) and display.get("text"):
        return str(display["text"])
    return str(place.get("name") or "Unknown place")


def _build_magic_search_skeleton(place: Dict[str, Any]) -> Dict[str, Any]:
    location = place.get("location") or {}
    hours = place.get("currentOpeningHours") or {}
    rating = place.get("rating")
    user_ratings_total = place.get("userRatingCount")
    return {
        "location_id": None,
        "google_place_id": place.get("id"),
        "name": _google_display_name(place),
        "vicinity": place.get("shortFormattedAddress") or place.get("formattedAddress"),
        "cuisine_primary": None,
        "rating": rating,
        "user_ratings_total": user_ratings_total,
        "price_level": _price_level_value(place.get("priceLevel")),
        "lat": location.get("latitude"),
        "lng": location.get("longitude"),
        "types": place.get("types") or [],
        "open_now": hours.get("openNow"),
        "vibe_vector": None,
        "dietary_requirement_vector": None,
        "app_engagement_score": 0.0,
        "google_baseline_score": compute_google_baseline_in_memory(rating, user_ratings_total),
        "video_insight_score": 0.0,
        "share_count": 0,
        "quality_bias": 0.0,
        "quality_score": compute_google_baseline_in_memory(rating, user_ratings_total) / 6.0,
        "has_app_signal": False,
        "image_stored": None,
        "image_unavailable": None,
        "extra_photos_stored": None,
    }
```

- [ ] **Step 4: Guard social/collab calls when effective weights are zero**

In `_rank_cached_candidates`, replace:

```python
    if user_id and not is_group_mode:
```

with:

```python
    wants_social = social_weight > 0
    wants_collab = collaborative_weight > 0

    if user_id and not is_group_mode and (wants_social or wants_collab):
```

Then wrap the two expensive calls:

```python
        if wants_social:
            social_scores, friend_attributions = compute_social_scores(
                user_id, candidate_location_ids, supabase
            )
            has_friends = any(s > 0 for s in social_scores.values())

        if wants_collab:
            collab_scores = compute_collaborative_scores(
                user_id, candidate_location_ids, supabase
            )
```

Keep the `action_count` block inside the same outer branch so adaptive weights still know whether this user has history when either social or collab is enabled. For magic search both weights will be zero, so this block is skipped.

- [ ] **Step 5: Rewrite `magic_search` to rank before persistence**

Replace the current Text Search and per-place persistence flow in `magic_search` with this structure:

```python
    cache_service = get_cache_service()
    user_profile = cache_service.get_or_build_user_profile(request.user_id, supabase)
    if user_profile is None:
        raise HTTPException(status_code=404, detail=f"User '{request.user_id}' not found")

    try:
        places, api_calls = await text_search_async(
            query=request.prompt,
            place_types=_FOOD_TYPES,
            lat=request.latitude,
            lng=request.longitude,
            radius_m=(request.radius_km or 2.0) * 1000,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
```

Build food skeletons:

```python
    skeletons_by_gid: Dict[str, Dict[str, Any]] = {}
    for place in places:
        gid = place.get("id")
        if not gid:
            continue
        types_raw = place.get("types", [])
        if types_raw and not any(t in _FOOD_TYPES_SET for t in types_raw):
            continue
        skeleton = _build_magic_search_skeleton(place)
        if skeleton.get("lat") is None or skeleton.get("lng") is None:
            continue
        skeletons_by_gid[gid] = skeleton

    existing_by_gid = supabase.get_locations_by_google_place_ids(list(skeletons_by_gid))
    existing_ids = [
        row["location_id"]
        for row in existing_by_gid.values()
        if row.get("location_id") is not None
    ]
    existing_rows_by_id = {
        row["location_id"]: row
        for row in supabase.get_locations_by_ids(existing_ids)
    }

    candidates: List[Dict[str, Any]] = []
    for gid, skeleton in skeletons_by_gid.items():
        existing = existing_by_gid.get(gid)
        if existing:
            full_row = existing_rows_by_id.get(existing["location_id"], existing)
            merged = {**skeleton, **full_row}
            candidates.append(merged)
        else:
            candidates.append(skeleton)

    candidates = [c for c in candidates if c.get("open_now") is not False]
```

Rank with no social/collab database calls:

```python
    scored = _rank_cached_candidates(
        candidates=candidates,
        request_lat=request.latitude,
        request_lng=request.longitude,
        request_radius_km=request.radius_km or 2.0,
        quality_weight=0.30,
        vibe_weight=0.25,
        dietary_weight=0.10,
        max_results=request.max_results or 20,
        user_id=request.user_id,
        social_weight=0.0,
        collaborative_weight=0.0,
        user_profile=user_profile,
    )
```

Enrich unknown top-K in parallel before constructing the response:

```python
    async def ensure_location_id(candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if candidate.get("location_id") is not None:
            return candidate
        gid = candidate.get("google_place_id")
        if not gid:
            return None
        created = await asyncio.to_thread(fetch_google_place_details, gid)
        if not created or created.get("location_id") is None:
            logger.warning("Magic search top result could not be persisted: gid=%s", gid)
            return None
        enriched = {**candidate, **created}
        enriched["vibe_score"] = candidate.get("vibe_score", 0.0)
        enriched["dietary_score"] = candidate.get("dietary_score", 0.0)
        enriched["quality_score"] = candidate.get("quality_score", 0.0)
        enriched["social_score"] = candidate.get("social_score", 0.0)
        enriched["collaborative_score"] = candidate.get("collaborative_score", 0.0)
        enriched["final_score"] = candidate.get("final_score", 0.0)
        enriched["rank"] = candidate.get("rank", 0)
        return enriched

    persisted_scored = [
        row
        for row in await asyncio.gather(*(ensure_location_id(candidate) for candidate in scored))
        if row is not None
    ]
```

Build `LocationRecommendation` from `persisted_scored`. Keep `location_id=int(candidate["location_id"])`; do not add a pending response field in this task.

- [ ] **Step 6: Run magic-search tests**

Run:

```bash
pytest tests/test_magic_search_fast_path.py tests/test_magic_search_quality_score.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/pinit/api/routers/proximal.py src/pinit/api/services/proximal_service.py tests/test_magic_search_fast_path.py
git commit -m "feat: rank magic search before top result enrichment"
```

## Task 5: Add Cache Hit/Miss Metrics

**Files:**
- Create: `src/pinit/api/services/cache_metrics.py`
- Modify: `src/pinit/api/services/cache_service.py`
- Modify: `src/pinit/api/routers/proximal.py`
- Test: `tests/test_cache_metrics.py`

- [ ] **Step 1: Write the metrics unit test**

Create `tests/test_cache_metrics.py`:

```python
import unittest

from pinit.api.services.cache_metrics import cache_metrics


class CacheMetricsTests(unittest.TestCase):
    def test_snapshot_reports_counts_and_hit_rate_by_endpoint(self) -> None:
        cache_metrics.reset()
        cache_metrics.increment("proximal", "hit")
        cache_metrics.increment("proximal", "miss")
        cache_metrics.increment("proximal", "hit")
        cache_metrics.increment("magic", "miss")

        snapshot = cache_metrics.snapshot()

        self.assertEqual(snapshot["proximal"]["hit"], 2)
        self.assertEqual(snapshot["proximal"]["miss"], 1)
        self.assertAlmostEqual(snapshot["proximal"]["hit_rate"], 2 / 3)
        self.assertEqual(snapshot["magic"]["hit"], 0)
        self.assertEqual(snapshot["magic"]["miss"], 1)
        self.assertEqual(snapshot["magic"]["total"], 1)
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
pytest tests/test_cache_metrics.py -q
```

Expected: FAIL because `cache_metrics.py` does not exist.

- [ ] **Step 3: Add the metrics service**

Create `src/pinit/api/services/cache_metrics.py`:

```python
from __future__ import annotations

from collections import Counter
from threading import Lock
from typing import Dict


class CacheMetrics:
    def __init__(self) -> None:
        self._lock = Lock()
        self._counts: Counter[tuple[str, str]] = Counter()

    def increment(self, endpoint: str, outcome: str) -> None:
        if outcome not in {"hit", "miss"}:
            raise ValueError(f"Unsupported cache metric outcome: {outcome}")
        with self._lock:
            self._counts[(endpoint, outcome)] += 1

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        with self._lock:
            endpoints = {endpoint for endpoint, _ in self._counts}
            result: Dict[str, Dict[str, float]] = {}
            for endpoint in sorted(endpoints):
                hits = self._counts[(endpoint, "hit")]
                misses = self._counts[(endpoint, "miss")]
                total = hits + misses
                result[endpoint] = {
                    "hit": hits,
                    "miss": misses,
                    "total": total,
                    "hit_rate": hits / total if total else 0.0,
                }
            return result

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


cache_metrics = CacheMetrics()
```

- [ ] **Step 4: Wire metrics into cache lookup**

In `src/pinit/api/services/cache_service.py`, import `cache_metrics` and change the signature:

```python
    def get_cached_recommendations(
        self,
        center_lat: float,
        center_lng: float,
        request_radius_km: float = 2.0,
        endpoint: str = "unknown",
    ) -> Optional[Dict[str, Any]]:
```

Before every `return data` hit:

```python
                        cache_metrics.increment(endpoint, "hit")
                        return data
```

Before the normal miss return:

```python
            cache_metrics.increment(endpoint, "miss")
            return None
```

Before Redis/error returns inside exception handlers:

```python
            cache_metrics.increment(endpoint, "miss")
            return None
```

In `_connect_redis`, after the successful connection log, add:

```python
            logger.info(
                "Cache config: coordinate_precision=%d large_radius_km=%.1f unfiltered_cache_ttl=%ds",
                self.config.coordinate_precision,
                self.config.large_radius_km,
                self.config.unfiltered_cache_ttl,
            )
            if self.config.coordinate_precision >= 2:
                logger.warning(
                    "Cache coordinate_precision=%d may fragment cache keys in dense areas",
                    self.config.coordinate_precision,
                )
```

- [ ] **Step 5: Pass endpoint labels from routers and expose stats**

In `src/pinit/api/routers/proximal.py`, update calls:

```python
    cached_data = cache_service.get_cached_recommendations(
        request.latitude, request.longitude, request.radius_km, endpoint="proximal"
    )
```

and:

```python
    cached_data = cache_service.get_cached_recommendations(
        request.latitude, request.longitude, request.radius_km, endpoint="bubble"
    )
```

Add a route:

```python
@router.get("/cache/stats")
async def get_cache_stats() -> Dict[str, Any]:
    cache = get_cache_service()
    stats = cache.get_cache_stats()
    stats["app_metrics"] = cache_metrics.snapshot()
    return stats
```

Import `cache_metrics` in the router.

- [ ] **Step 6: Run metrics tests**

Run:

```bash
pytest tests/test_cache_metrics.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/pinit/api/services/cache_metrics.py src/pinit/api/services/cache_service.py src/pinit/api/routers/proximal.py tests/test_cache_metrics.py
git commit -m "feat: add endpoint cache hit metrics"
```

## Task 6: Schedule Cache Warm-Up In Process

**Files:**
- Modify: `src/pinit/cli/warm_cache.py`
- Modify: `src/pinit/api/main.py`
- Test: `tests/test_cache_metrics.py`

- [ ] **Step 1: Add warm-loop test**

Append to `tests/test_cache_metrics.py`:

```python
from unittest.mock import patch


class WarmCacheLoopTests(unittest.TestCase):
    def test_run_warm_cache_pass_uses_lock_and_runs_snapshot_then_zones(self) -> None:
        from pinit.cli import warm_cache

        events = []

        with patch.object(warm_cache, "acquire_warm_cache_lock", return_value=True), \
             patch.object(warm_cache, "refresh_lpa_snapshot", side_effect=lambda: events.append("snapshot") or True), \
             patch.object(warm_cache, "warm_zones", side_effect=lambda zone_set, radius_km: events.append((zone_set, radius_km)) or 11):
            result = warm_cache.run_warm_cache_pass(zone_set="london", radius_km=15.0)

        self.assertTrue(result)
        self.assertEqual(events, ["snapshot", ("london", 15.0)])

    def test_run_warm_cache_pass_skips_when_lock_is_held(self) -> None:
        from pinit.cli import warm_cache

        with patch.object(warm_cache, "acquire_warm_cache_lock", return_value=False), \
             patch.object(warm_cache, "refresh_lpa_snapshot") as refresh:
            result = warm_cache.run_warm_cache_pass(zone_set="london", radius_km=15.0)

        self.assertFalse(result)
        refresh.assert_not_called()
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
pytest tests/test_cache_metrics.py::WarmCacheLoopTests -q
```

Expected: FAIL because the warm pass helpers do not exist.

- [ ] **Step 3: Add reusable warm pass and async loop**

In `src/pinit/cli/warm_cache.py`, add imports:

```python
import asyncio
import random
from uuid import uuid4
```

Add:

```python
def acquire_warm_cache_lock(ttl_s: int = 240) -> bool:
    cache = get_cache_service()
    if not cache.is_available or cache._redis_client is None:
        return False
    return bool(cache._redis_client.set("warm:lock", str(uuid4()), nx=True, ex=ttl_s))


def run_warm_cache_pass(zone_set: str = "london", radius_km: float = 15.0) -> bool:
    if not acquire_warm_cache_lock():
        logger.info("Warm cache pass skipped because warm:lock is held")
        return False

    snapshot_ok = refresh_lpa_snapshot()
    warmed = warm_zones(zone_set, radius_km=radius_km)
    return bool(snapshot_ok and warmed > 0)


async def warm_cache_loop(
    interval_s: int = 300,
    jitter_s: int = 30,
    zone_set: str = "london",
    radius_km: float = 15.0,
) -> None:
    while True:
        try:
            await asyncio.to_thread(run_warm_cache_pass, zone_set, radius_km)
        except Exception as exc:
            logger.exception("Warm cache loop pass failed: %s", exc)
        await asyncio.sleep(interval_s + random.uniform(-jitter_s, jitter_s))
```

- [ ] **Step 4: Start and cancel the loop in FastAPI**

In `src/pinit/api/main.py`, import `asyncio`, `suppress`, and `warm_cache_loop`:

```python
import asyncio
from contextlib import suppress
from pinit.cli.warm_cache import warm_cache_loop
```

Add a module global:

```python
_warm_cache_task: asyncio.Task | None = None
```

In `startup_background_workers`, after starting background workers:

```python
    global _warm_cache_task
    if os.getenv("PINIT_WARM_CACHE_ENABLED", "true").lower() == "true":
        _warm_cache_task = asyncio.create_task(warm_cache_loop())
        logger.info("Started warm cache scheduler")
```

In `shutdown_background_workers`, before returning:

```python
    global _warm_cache_task
    if _warm_cache_task is not None:
        _warm_cache_task.cancel()
        with suppress(asyncio.CancelledError):
            await _warm_cache_task
        _warm_cache_task = None
        logger.info("Stopped warm cache scheduler")
```

- [ ] **Step 5: Run warm-loop tests**

Run:

```bash
pytest tests/test_cache_metrics.py::WarmCacheLoopTests -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pinit/cli/warm_cache.py src/pinit/api/main.py tests/test_cache_metrics.py
git commit -m "feat: schedule proximal cache warmup"
```

## Task 7: Integration Verification

**Files:**
- No code changes expected.

- [ ] **Step 1: Run focused Python tests**

Run:

```bash
pytest tests/test_magic_search_fast_path.py tests/test_magic_search_quality_score.py tests/test_cache_metrics.py -q
```

Expected: PASS.

- [ ] **Step 2: Run existing router/service tests if present**

Run:

```bash
pytest tests -q
```

Expected: PASS. If unrelated existing failures appear, capture the failing test names and error summaries before deciding whether they block this change.

- [ ] **Step 3: Run a local cold-path timing check**

Start the API using the project’s existing command, then call magic search with a fresh prompt and London coordinates:

```bash
: "${PINIT_MAGIC_TEST_USER_ID:?set PINIT_MAGIC_TEST_USER_ID to a valid local dev user id}"
time curl -sS -X POST http://localhost:8000/locations/magic-search \
  -H 'Content-Type: application/json' \
  -d "{\"user_id\":\"${PINIT_MAGIC_TEST_USER_ID}\",\"latitude\":51.5137,\"longitude\":-0.1310,\"prompt\":\"good casual ramen near soho\",\"radius_km\":2.0,\"max_results\":20}" \
  | python -m json.tool >/tmp/magic-search-response.json
```

Expected:
- Wall time is close to one Google Text Search shard plus parallel top-K details, not six serial Text Search calls plus all-candidate details.
- Every recommendation has integer `location_id`.
- No recommendation has a `pending` field.

- [ ] **Step 4: Verify cache warm-up and metrics**

After the API has been running for at least six minutes:

```bash
curl -sS http://localhost:8000/cache/stats | python -m json.tool
```

Expected:
- `proximal_cache_keys` is greater than zero when Redis is available.
- `app_metrics` contains endpoint keys after traffic.
- Proximal or bubble hit/miss counts change after requests.

## Deferred Phase 2 Plan

These are deliberately excluded from Phase 1:

- Redis GEO index for `proximal:geo:*` keys. Add it after `/cache/stats` shows warm-cache hit patterns and key counts. The implementation should write a GEO member beside each cache payload, query `GEOSEARCH ... COUNT 8`, lazy-delete orphans, and bump the payload version from `v7` to `v8`.
- Magic-search Redis cache. Add it only after skeleton ranking is stable. Cache the pre-rank merged candidate set under a normalized prompt/location/radius key, then rerank per user.
- Background enrichment for the non-top-K long tail. Add it only after top-K enrichment and unique `google_place_id` writes are proven in production logs.
- Pending skeleton response contract. This needs a separate API/client design because the current schema requires `location_id: int`.

## Self-Review

- Spec coverage: The plan covers magic-search serial Google calls, all-candidate synchronous enrichment, missing uniqueness for concurrent inserts, empty cache warm-up, missing app-level cache metrics, and phase ordering for Redis GEO/magic cache.
- Placeholder scan: No implementation step relies on unspecified behavior; code snippets include concrete functions, signatures, commands, and expected outcomes.
- Type consistency: Skeleton candidates use `location_id=None` internally only; response construction still emits `LocationRecommendation.location_id` as an integer after top-K persistence.
