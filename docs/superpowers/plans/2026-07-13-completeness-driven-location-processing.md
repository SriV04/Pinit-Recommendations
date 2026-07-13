# Completeness-Driven Location Processing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every expanded-card open enqueue one durable, ordered location-completion request that reads Supabase, refreshes Google Places plus reviews at most every 30 days, and runs only missing downstream stages.

**Architecture:** A pure `LocationProcessingPlan` decides which stages are due from the canonical Supabase row. The existing Pub/Sub topic gains one `process_location` task routed to the menu-capable request-based worker; that worker executes Google, photo, emoji, content, and vibe work idempotently in dependency order. Flutter always queues canonical locations and uses Place ID rather than coordinates for its Maps fallback.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, Supabase Python client, Google Places API v1, Pub/Sub push, Cloud Run, pytest/unittest, Flutter/Dart, `package:http`, `flutter_test`.

---

## File Structure

Backend repository: `/Users/sriharshavitta/Projects/pinit-recommendations`

- Create `src/pinit/api/services/location_completeness.py`: pure completeness and 30-day freshness rules.
- Create `src/pinit/api/services/location_content_fallback.py`: fill story, cuisine, and dietary outputs when menu analysis cannot.
- Modify `src/pinit/api/schemas_location_tasks.py`: add the single `process_location` payload.
- Modify `src/pinit/api/services/proximal_service.py`: direct full Google refresh, review field mask, non-destructive merge, timestamp, and missing-Place-ID resolution.
- Modify `src/pinit/api/services/location_tasks.py`: execute one idempotent completion pass while retaining legacy handlers during cutover.
- Modify `src/pinit/api/schemas.py` and `src/pinit/api/routers/proximal.py`: expose `/locations/process` and make `/locations/add` publish the same payload.
- Modify `pubsub.sh` and `verify_pubsub.sh`: provision and verify `location-tasks-process_location` on the menu worker without changing request-based billing.
- Create `tests/test_location_completeness.py`, `tests/test_location_processing.py`, and `tests/test_location_google_refresh.py`.
- Modify `tests/test_api_endpoints.py`, `tests/test_location_tasks.py`, and `tests/test_pubsub_deployment_scripts.py`.

Flutter repository: `/Users/sriharshavitta/Projects/login`

- Modify `lib/services/recommendations_api.dart`: add `/locations/process` client method.
- Modify `lib/widgets/home/expanded_location_card.dart`: queue every canonical card open and use the Place-ID Maps fallback.
- Create `lib/widgets/home/expanded_card/helpers/google_maps_target.dart`: pure Maps URI selection.
- Modify `test/services/recommendations_api_test.dart`.
- Create `test/widgets/home/expanded_card/helpers/google_maps_target_test.dart`.

---

### Task 1: Pure completeness plan

**Files:**
- Create: `src/pinit/api/services/location_completeness.py`
- Create: `tests/test_location_completeness.py`

- [ ] **Step 1: Write failing freshness and stage tests**

Create tests covering a null timestamp, a timestamp exactly 30 days old, a fresh timestamp with optional Google nulls, explicit false/zero values, independent photo/emoji/content checks, and the two-field vibe completion rule. The public API exercised by the tests is:

```python
from datetime import datetime, timezone
from pinit.api.services.location_completeness import build_location_processing_plan

def test_fresh_google_fetch_accepts_optional_nulls() -> None:
    row = {
        "google_details_fetched_at": "2026-07-01T12:00:00+00:00",
        "website": None,
        "reviews": None,
        "image_stored": True,
        "emoji": "🍜",
        "generated_summary": "A compact noodle bar.",
        "cuisine_primary": "japanese",
        "dietary_requirement_vector": [0, 20, 80],
        "vibe_vector": [0.1, 0.8],
        "updated_vibe": True,
    }

    plan = build_location_processing_plan(
        row,
        now=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
    )

    assert plan.google_due is False
    assert plan.is_complete is True
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest -q tests/test_location_completeness.py`

Expected: collection fails with `ModuleNotFoundError` for `location_completeness`.

- [ ] **Step 3: Implement the pure plan**

Implement immutable decisions with a single 30-day constant:

```python
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
        return not any((
            self.google_due,
            self.photo_due,
            self.emoji_due,
            self.menu_due,
            self.content_fallback_due,
            self.vibe_due,
        ))
```

Parse Supabase timestamp strings as UTC, treat exactly 30 days as stale, treat `False` and `0` as present values, require either photo terminal flag, require all three content outputs, and require both vibe outputs. `menu_due` is true only when content is missing and a non-empty website exists; `content_fallback_due` is true whenever required content remains missing.

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `pytest -q tests/test_location_completeness.py`

Expected: all completeness tests pass.

- [ ] **Step 5: Commit the planner**

```bash
git add src/pinit/api/services/location_completeness.py tests/test_location_completeness.py
git commit -m "feat: define location completeness plan"
```

### Task 2: Google refresh, reviews, and timestamp

**Files:**
- Modify: `src/pinit/api/services/proximal_service.py`
- Create: `tests/test_location_google_refresh.py`

- [ ] **Step 1: Write failing Google refresh tests**

Use `unittest.mock.patch` around `requests.get` and the Supabase service. Assert:

```python
def test_full_refresh_requests_reviews_and_marks_success() -> None:
    result = refresh_location_from_google_place_details(42, "place-42")
    assert result is not None
    field_mask = fake_get.call_args.kwargs["headers"]["X-Goog-FieldMask"]
    assert "reviews" in field_mask.split(",")
    update = fake_supabase.update_location.call_args.kwargs
    assert update["google_details_fetched_at"].endswith("+00:00")

def test_omitted_optional_values_do_not_erase_existing_data() -> None:
    refresh_location_from_google_place_details(42, "place-42")
    update = fake_supabase.update_location.call_args.kwargs
    assert "website" not in update
    assert "reviews" not in update
    assert update["good_for_children"] is False
    assert update["price_level"] == 0
```

Add a resolver test proving an existing Place ID performs no Text Search and a missing ID accepts only one exact candidate near the stored coordinates.

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest -q tests/test_location_google_refresh.py`

Expected: timestamp and non-destructive merge assertions fail, and the resolver import is absent.

- [ ] **Step 3: Implement non-destructive full refresh**

Add an update-specific payload builder that removes `None`, empty strings, and empty containers but retains `False` and numeric zero. Add `google_details_fetched_at=datetime.now(timezone.utc).isoformat()` only after a valid Google payload is normalized. Keep create behavior unchanged so required insert fields remain explicit.

Implement:

```python
def resolve_google_place_id_for_location(row: Dict[str, Any]) -> Optional[str]:
    existing = str(row.get("google_place_id") or "").strip()
    if existing:
        return existing
    # POST Places Text Search with name, vicinity, and location bias.
    # Return a candidate only when normalized name/address and distance make it
    # unambiguous; otherwise return None.
```

The Details mask continues to include `reviews`, `reviewSummary`, photos, Maps URI, phone, editorial summary, hours, and all venue/serves booleans.

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `pytest -q tests/test_location_google_refresh.py`

Expected: all Google refresh tests pass.

- [ ] **Step 5: Commit Google refresh behavior**

```bash
git add src/pinit/api/services/proximal_service.py tests/test_location_google_refresh.py
git commit -m "feat: refresh Google location data every thirty days"
```

### Task 3: Google/review content fallback

**Files:**
- Create: `src/pinit/api/services/location_content_fallback.py`
- Create: `tests/test_location_content_fallback.py`

- [ ] **Step 1: Write failing fallback tests**

Test the write boundary with an injected analyzer so network calls are not needed:

```python
async def test_fallback_fills_only_missing_content() -> None:
    row = {
        "location_id": 42,
        "generated_summary": None,
        "cuisine_primary": "thai",
        "dietary_requirement_vector": None,
        "reviews": [{"rating": 5, "text": "Great vegan curries"}],
    }
    result = LocationContentFallback(
        summary="A lively neighbourhood Thai restaurant.",
        cuisine_primary="thai",
        dietary_requirement_vector=[70, 80, 20],
    )

    await fill_missing_location_content(row, analyzer=AsyncMock(return_value=result))

    update = fake_supabase.update_location.call_args.kwargs
    assert update["generated_summary"] == result.summary
    assert update["dietary_requirement_vector"] == result.dietary_requirement_vector
    assert "cuisine_primary" not in update
    assert update["menu_analysis_confidence"] == "google_fallback"
```

Also assert no recommendation-dish field is written and an empty/invalid analysis raises so Pub/Sub can retry.

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest -q tests/test_location_content_fallback.py`

Expected: collection fails because the fallback module does not exist.

- [ ] **Step 3: Implement the fallback**

Create a Pydantic result model and an async analyzer using the configured xAI client. Provide the analyzer with only the canonical row fields approved in the spec: name, address, types, Google summaries, attributes, hours, rating, and reviews. Require a concise factual story, a cuisine label, and a dietary vector in the repository's existing dietary index order. The update function filters its patch against already-present row values and never writes `reccomended_dishes`.

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `pytest -q tests/test_location_content_fallback.py`

Expected: all fallback tests pass.

- [ ] **Step 5: Commit fallback content generation**

```bash
git add src/pinit/api/services/location_content_fallback.py tests/test_location_content_fallback.py
git commit -m "feat: fill location story from Google evidence"
```

### Task 4: Single idempotent worker task

**Files:**
- Modify: `src/pinit/api/schemas_location_tasks.py`
- Modify: `src/pinit/api/services/location_tasks.py`
- Modify: `tests/test_location_tasks.py`
- Create: `tests/test_location_processing.py`

- [ ] **Step 1: Write failing payload and orchestration tests**

Add `ProcessLocationPayload` expectations and use async mocks to assert this sequence:

```python
payload = ProcessLocationPayload(
    task_type="process_location",
    request_id="request-1",
    location_id=42,
    google_place_id="place-42",
    source="in-app",
)

await process_location_task(payload)

assert fake_supabase.get_location.call_count >= 2
refresh_google.assert_awaited_once()
photo.assert_awaited_once()
emoji.assert_awaited_once()
menu.assert_awaited_once()
fallback.assert_awaited_once()
vibe.assert_awaited_once()
```

Separate tests prove a complete row makes no external calls, Google failure raises without downstream work, a no-website row goes directly to fallback, and retry after partial persistence skips completed stages.

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest -q tests/test_location_tasks.py tests/test_location_processing.py`

Expected: `ProcessLocationPayload` and `process_location_task` imports fail.

- [ ] **Step 3: Add the payload and processor**

Extend the discriminated union with:

```python
class ProcessLocationPayload(LocationTaskPayloadBase):
    task_type: Literal["process_location"]
```

Allow the payload Place ID to be empty because the canonical row may require resolution. In `handle_location_task`, route `process_location` to a processor that reloads and replans after Google and after content work. Reuse `photos_task`, `emoji_task`, and `generate_vibe_tags_for_location`; call menu analysis only when the plan says it is runnable; then call the fallback only for still-missing content. Keep legacy task routes during cutover.

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `pytest -q tests/test_location_tasks.py tests/test_location_processing.py`

Expected: all worker and orchestration tests pass.

- [ ] **Step 5: Commit the single worker path**

```bash
git add src/pinit/api/schemas_location_tasks.py src/pinit/api/services/location_tasks.py tests/test_location_tasks.py tests/test_location_processing.py
git commit -m "feat: process location gaps in one worker task"
```

### Task 5: API publishing contract

**Files:**
- Modify: `src/pinit/api/schemas.py`
- Modify: `src/pinit/api/routers/proximal.py`
- Modify: `tests/test_api_endpoints.py`

- [ ] **Step 1: Write failing endpoint tests**

Add tests asserting:

```python
response = client.post(
    "/locations/process",
    json={"location_id": 3001, "google_place_id": "known-google-place", "source": "in-app"},
)
assert response.status_code == 202
assert FakeDispatcher.dispatched[0].task_type == "process_location"
assert FakeDispatcher.dispatched[0].location_id == 3001
```

Update `/locations/add` tests so both existing and newly-created rows dispatch exactly one `process_location` payload. Assert a missing/non-positive location ID returns 422 or 404 and no message.

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest -q tests/test_api_endpoints.py -k 'locations_add or locations_process'`

Expected: `/locations/process` returns 404 and old add tests observe `pipeline`.

- [ ] **Step 3: Implement the endpoint and shared publisher**

Add `ProcessLocationRequest` and a 202 response model. Extract a single `_dispatch_location_processing(...)` helper selecting Pub/Sub or in-process delivery. `/locations/process` validates the canonical row and dispatches. `/locations/add` retains basic creation and social-insight upsert, but async and inline modes both invoke the new processor rather than source-specific pipeline branching.

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `pytest -q tests/test_api_endpoints.py -k 'locations_add or locations_process'`

Expected: all selected endpoint tests pass.

- [ ] **Step 5: Commit the API contract**

```bash
git add src/pinit/api/schemas.py src/pinit/api/routers/proximal.py tests/test_api_endpoints.py
git commit -m "feat: enqueue canonical location processing"
```

### Task 6: Pub/Sub cutover configuration

**Files:**
- Modify: `pubsub.sh`
- Modify: `verify_pubsub.sh`
- Modify: `tests/test_pubsub_deployment_scripts.py`
- Modify: `docs/deploying-location-pubsub.md`

- [ ] **Step 1: Write failing script invariant tests**

Require `location-tasks-process_location`, filter `attributes.task_type="process_location"`, menu-worker push URL, ordering, retry/dead-letter policy, and the existing `--cpu-throttling --min-instances 0` invariants. Require verification to check the new subscription while continuing to recognize legacy subscriptions during the rollback window.

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest -q tests/test_pubsub_deployment_scripts.py`

Expected: assertions for `process_location` subscription fail.

- [ ] **Step 3: Update provisioning and verification**

Add the new filtered subscription to the menu worker with the same authenticated push identity, 600-second acknowledgement deadline, retry policy, ten-attempt dead-letter policy, ordering, and no expiration. Do not delete legacy subscriptions in this implementation. Document the cutover and explicit backlog check required before their eventual removal.

- [ ] **Step 4: Verify scripts**

Run:

```bash
bash -n deploy.sh pubsub.sh verify_pubsub.sh
pytest -q tests/test_pubsub_deployment_scripts.py
```

Expected: shell syntax succeeds and all deployment-script tests pass.

- [ ] **Step 5: Commit infrastructure support**

```bash
git add pubsub.sh verify_pubsub.sh tests/test_pubsub_deployment_scripts.py docs/deploying-location-pubsub.md
git commit -m "feat: route complete location processing through Pub/Sub"
```

### Task 7: Flutter trigger and Maps destination

**Files:**
- Modify: `/Users/sriharshavitta/Projects/login/lib/services/recommendations_api.dart`
- Modify: `/Users/sriharshavitta/Projects/login/lib/widgets/home/expanded_location_card.dart`
- Create: `/Users/sriharshavitta/Projects/login/lib/widgets/home/expanded_card/helpers/google_maps_target.dart`
- Modify: `/Users/sriharshavitta/Projects/login/test/services/recommendations_api_test.dart`
- Create: `/Users/sriharshavitta/Projects/login/test/widgets/home/expanded_card/helpers/google_maps_target_test.dart`

- [ ] **Step 1: Write failing client and Maps tests**

Add a service test expecting:

```dart
await api.processLocation(
  locationId: 42,
  googlePlaceId: 'place-42',
  source: 'in-app',
);
expect(requestUri.path, '/locations/process');
expect(payload, {
  'location_id': 42,
  'google_place_id': 'place-42',
  'source': 'in-app',
});
```

Add helper tests proving canonical URI wins, fallback includes an encoded name and `query_place_id`, and coordinates are used only without a Place ID.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
cd /Users/sriharshavitta/Projects/login
flutter test test/services/recommendations_api_test.dart test/widgets/home/expanded_card/helpers/google_maps_target_test.dart
```

Expected: `processLocation` and `resolveGoogleMapsTarget` are undefined.

- [ ] **Step 3: Implement the client, trigger, and helper**

Add `_processLocationPath = '/locations/process'` and `Future<void> processLocation(...)`. Replace `_enrichLocationIfNeeded` with `_requestLocationProcessing`, remove the website gate, and call it from `initState` for every positive canonical ID. The Maps helper returns in order: parsed `googleMapsUri`; a `https://www.google.com/maps/search/?api=1&query=<name>&query_place_id=<id>` URI; then the current coordinate query.

- [ ] **Step 4: Run focused Flutter tests and analyzer**

Run:

```bash
cd /Users/sriharshavitta/Projects/login
dart format lib/services/recommendations_api.dart lib/widgets/home/expanded_location_card.dart lib/widgets/home/expanded_card/helpers/google_maps_target.dart test/services/recommendations_api_test.dart test/widgets/home/expanded_card/helpers/google_maps_target_test.dart
flutter test test/services/recommendations_api_test.dart test/widgets/home/expanded_card/helpers/google_maps_target_test.dart test/widgets/home/expanded_location_card_config_test.dart
flutter analyze lib/services/recommendations_api.dart lib/widgets/home/expanded_location_card.dart lib/widgets/home/expanded_card/helpers/google_maps_target.dart
```

Expected: tests pass and analyzer reports no issues in changed files.

- [ ] **Step 5: Commit Flutter changes**

```bash
cd /Users/sriharshavitta/Projects/login
git add lib/services/recommendations_api.dart lib/widgets/home/expanded_location_card.dart lib/widgets/home/expanded_card/helpers/google_maps_target.dart test/services/recommendations_api_test.dart test/widgets/home/expanded_card/helpers/google_maps_target_test.dart
git commit -m "feat: process every expanded location"
```

### Task 8: Full verification and deployment readiness

**Files:**
- Modify only if verification exposes a tested defect in the files above.

- [ ] **Step 1: Run backend focused suite**

```bash
cd /Users/sriharshavitta/Projects/pinit-recommendations
pytest -q tests/test_location_completeness.py tests/test_location_google_refresh.py tests/test_location_content_fallback.py tests/test_location_processing.py tests/test_location_tasks.py tests/test_api_endpoints.py tests/test_pubsub_deployment_scripts.py
```

Expected: all focused tests pass.

- [ ] **Step 2: Run syntax and import checks**

```bash
bash -n deploy.sh pubsub.sh verify_pubsub.sh
python -m compileall -q src/pinit/api src/pinit/worker
git diff --check
```

Expected: every command exits zero.

- [ ] **Step 3: Run Flutter focused suite again**

```bash
cd /Users/sriharshavitta/Projects/login
flutter test test/services/recommendations_api_test.dart test/widgets/home/expanded_card/helpers/google_maps_target_test.dart test/widgets/home/expanded_location_card_config_test.dart
git diff --check
```

Expected: tests pass and both worktrees are clean after commits.

- [ ] **Step 4: Review production deployment diff without deploying**

Confirm the generated configuration keeps `--cpu-throttling`, `--min-instances 0`, authenticated push, ordering, retry/dead-letter policy, and enables API publishing only after subscription verification. Production deployment requires an explicit deployment checkpoint after local implementation verification.

