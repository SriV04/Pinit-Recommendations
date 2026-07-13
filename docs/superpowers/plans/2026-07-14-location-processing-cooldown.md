# Location Processing Cooldown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admit an expanded location to processing only when its fresh Supabase row needs work and its last accepted processing request is at least 30 days old.

**Architecture:** Flutter performs a lightweight fresh-row eligibility check before calling the API. The backend remains authoritative by acquiring a short Supabase claim, publishing one canonical Pub/Sub task, and recording `location_processing_queued_at` only after acceptance; the worker repairs that record if the API exits after publishing. The existing completeness planner and Pub/Sub retry behavior remain unchanged.

**Tech Stack:** PostgreSQL/Supabase RPCs, Python 3.11/FastAPI/Pydantic, Google Cloud Pub/Sub and Cloud Run, Flutter/Dart, pytest, flutter_test.

**Execution note:** The user explicitly requested work on `main`, so execute in the existing main checkouts rather than creating a worktree.

---

## File structure

Backend repository: `/Users/sriharshavitta/Projects/pinit-recommendations`

- Create through `supabase migration new`: `supabase/migrations/*_add_location_processing_cooldown.sql` — schema, indexes, and service-role-only claim/complete/release RPCs.
- Create: `tests/test_location_processing_migration.py` — static migration security and contract checks.
- Create: `src/pinit/api/services/location_processing_admission.py` — one bounded admission operation around claim, dispatch, completion, and rollback.
- Create: `tests/test_location_processing_admission.py` — admission red/green tests independent of FastAPI.
- Modify: `src/pinit/integrations/supabase.py:527-583` — typed wrappers for the three RPCs.
- Create: `tests/test_location_processing_supabase.py` — exact RPC names and parameter serialization.
- Modify: `src/pinit/api/routers/proximal.py:1653-1805` — route both canonical processing entry points through admission.
- Modify: `src/pinit/api/services/location_tasks.py:81-99` — worker-side tracker repair before processing.
- Modify: `tests/test_api_endpoints.py:130-210,442-526` — cooldown API coverage.
- Modify: `tests/test_location_tasks.py:1-90` — worker repair coverage.

Flutter repository: `/Users/sriharshavitta/Projects/login`

- Create: `lib/services/location_processing_trigger.dart` — fresh Supabase snapshot fetch, defensive parsing, eligibility predicate, and API trigger.
- Create: `test/services/location_processing_trigger_test.dart` — all cooldown and major-gap cases.
- Modify: `lib/widgets/home/expanded_location_card.dart:115-193,630-644` — call the trigger silently on open.
- Modify: `test/widgets/home/expanded_location_card_config_test.dart` — pin the injectable trigger seam without changing visual behavior.

---

### Task 1: Add the atomic Supabase cooldown contract

**Files:**
- Create through CLI: `supabase/migrations/*_add_location_processing_cooldown.sql`
- Create: `tests/test_location_processing_migration.py`

- [ ] **Step 1: Write the failing migration contract test**

```python
from pathlib import Path


MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase" / "migrations"


def _cooldown_sql() -> str:
    matches = sorted(MIGRATIONS.glob("*_add_location_processing_cooldown.sql"))
    assert len(matches) == 1, matches
    return matches[0].read_text().lower()


def test_location_processing_cooldown_migration_is_atomic_and_private() -> None:
    sql = _cooldown_sql()
    assert "location_processing_queued_at" in sql
    assert "location_processing_claim_id" in sql
    assert "location_processing_claimed_at" in sql
    assert "claim_location_processing" in sql
    assert "complete_location_processing_queue" in sql
    assert "release_location_processing_claim" in sql
    assert sql.count("security invoker") == 3
    assert sql.count("revoke all on function") == 3
    assert sql.count("to service_role") == 3
    assert "to anon" not in sql
    assert "to authenticated" not in sql
    assert "2592000" in sql
    assert "300" in sql
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
cd /Users/sriharshavitta/Projects/pinit-recommendations
pytest -q tests/test_location_processing_migration.py
```

Expected: FAIL because no cooldown migration exists.

- [ ] **Step 3: Create the migration with the Supabase CLI**

Run:

```bash
cd /Users/sriharshavitta/Projects/pinit-recommendations
supabase migration new add_location_processing_cooldown
```

Use the exact path printed by the CLI for the SQL below; do not hand-invent a timestamped filename.

```sql
alter table public.locations
  add column if not exists location_processing_queued_at timestamptz,
  add column if not exists location_processing_claim_id text,
  add column if not exists location_processing_claimed_at timestamptz;

create index if not exists idx_locations_processing_queued_at
  on public.locations (location_processing_queued_at)
  where location_processing_queued_at is not null;

create index if not exists idx_locations_processing_claimed_at
  on public.locations (location_processing_claimed_at)
  where location_processing_claimed_at is not null;

create or replace function public.claim_location_processing(
  p_location_id integer,
  p_request_id text,
  p_cooldown_seconds integer default 2592000,
  p_claim_stale_after_seconds integer default 300
)
returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
  did_claim boolean := false;
begin
  update public.locations
  set
    location_processing_claim_id = p_request_id,
    location_processing_claimed_at = now()
  where location_id = p_location_id
    and (
      location_processing_queued_at is null
      or location_processing_queued_at <= now() - make_interval(secs => p_cooldown_seconds)
    )
    and (
      location_processing_claimed_at is null
      or location_processing_claimed_at <= now() - make_interval(secs => p_claim_stale_after_seconds)
    )
  returning true into did_claim;

  return coalesce(did_claim, false);
end;
$$;

create or replace function public.complete_location_processing_queue(
  p_location_id integer,
  p_request_id text
)
returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
  did_complete boolean := false;
begin
  update public.locations
  set
    location_processing_queued_at = now(),
    location_processing_claim_id = null,
    location_processing_claimed_at = null
  where location_id = p_location_id
    and location_processing_claim_id = p_request_id
  returning true into did_complete;

  return coalesce(did_complete, false);
end;
$$;

create or replace function public.release_location_processing_claim(
  p_location_id integer,
  p_request_id text
)
returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
  did_release boolean := false;
begin
  update public.locations
  set
    location_processing_claim_id = null,
    location_processing_claimed_at = null
  where location_id = p_location_id
    and location_processing_claim_id = p_request_id
  returning true into did_release;

  return coalesce(did_release, false);
end;
$$;

revoke all on function public.claim_location_processing(integer, text, integer, integer)
  from public, anon, authenticated;
grant execute on function public.claim_location_processing(integer, text, integer, integer)
  to service_role;

revoke all on function public.complete_location_processing_queue(integer, text)
  from public, anon, authenticated;
grant execute on function public.complete_location_processing_queue(integer, text)
  to service_role;

revoke all on function public.release_location_processing_claim(integer, text)
  from public, anon, authenticated;
grant execute on function public.release_location_processing_claim(integer, text)
  to service_role;
```

- [ ] **Step 4: Run the migration contract test and verify GREEN**

Run: `pytest -q tests/test_location_processing_migration.py`

Expected: `1 passed`.

- [ ] **Step 5: Commit the schema contract**

```bash
git add supabase/migrations tests/test_location_processing_migration.py
git commit -m "feat: add location processing cooldown claim"
```

---

### Task 2: Implement backend admission as a tested unit

**Files:**
- Create: `src/pinit/api/services/location_processing_admission.py`
- Create: `tests/test_location_processing_admission.py`
- Modify: `src/pinit/integrations/supabase.py:527-583`
- Create: `tests/test_location_processing_supabase.py`

- [ ] **Step 1: Write failing admission tests**

```python
import unittest
from unittest.mock import AsyncMock

from pinit.api.schemas_location_tasks import ProcessLocationPayload
from pinit.api.services.location_processing_admission import admit_location_processing


def _payload() -> ProcessLocationPayload:
    return ProcessLocationPayload(
        task_type="process_location",
        request_id="request-42",
        location_id=42,
        google_place_id="place-42",
        source="expanded-card-open",
    )


class _FakeSupabase:
    def __init__(self, claimed: bool = True) -> None:
        self.claimed = claimed
        self.claim_calls: list[tuple] = []
        self.complete_calls: list[tuple] = []
        self.release_calls: list[tuple] = []

    def claim_location_processing(self, location_id, request_id, **kwargs):
        self.claim_calls.append((location_id, request_id, kwargs))
        return self.claimed

    def complete_location_processing_queue(self, location_id, request_id):
        self.complete_calls.append((location_id, request_id))
        return True

    def release_location_processing_claim(self, location_id, request_id):
        self.release_calls.append((location_id, request_id))
        return True


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_denied_claim_does_not_dispatch(self) -> None:
        supabase = _FakeSupabase(claimed=False)
        dispatch = AsyncMock()
        result = await admit_location_processing(
            _payload(), supabase=supabase, dispatch=dispatch
        )
        self.assertFalse(result.queued)
        dispatch.assert_not_awaited()
        self.assertEqual(supabase.complete_calls, [])

    async def test_successful_dispatch_completes_tracker(self) -> None:
        supabase = _FakeSupabase()
        dispatch = AsyncMock()
        result = await admit_location_processing(
            _payload(), supabase=supabase, dispatch=dispatch
        )
        self.assertTrue(result.queued)
        dispatch.assert_awaited_once_with(_payload())
        self.assertEqual(supabase.complete_calls, [(42, "request-42")])
        self.assertEqual(supabase.release_calls, [])

    async def test_failed_dispatch_releases_claim(self) -> None:
        supabase = _FakeSupabase()
        dispatch = AsyncMock(side_effect=RuntimeError("publish failed"))
        with self.assertRaisesRegex(RuntimeError, "publish failed"):
            await admit_location_processing(
                _payload(), supabase=supabase, dispatch=dispatch
            )
        self.assertEqual(supabase.complete_calls, [])
        self.assertEqual(supabase.release_calls, [(42, "request-42")])
```

- [ ] **Step 2: Run the admission tests and verify RED**

Run: `pytest -q tests/test_location_processing_admission.py`

Expected: collection FAIL because `location_processing_admission` does not exist.

- [ ] **Step 3: Implement the minimal admission service**

```python
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from pinit.api.schemas_location_tasks import ProcessLocationPayload
from pinit.integrations.supabase import SupabaseService

logger = logging.getLogger(__name__)
COOLDOWN_SECONDS = 30 * 24 * 60 * 60
CLAIM_STALE_AFTER_SECONDS = 5 * 60

Dispatch = Callable[[ProcessLocationPayload], Awaitable[None]]


@dataclass(frozen=True)
class LocationProcessingAdmissionResult:
    queued: bool
    request_id: str


async def admit_location_processing(
    payload: ProcessLocationPayload,
    *,
    supabase: SupabaseService,
    dispatch: Dispatch,
) -> LocationProcessingAdmissionResult:
    claimed = await asyncio.to_thread(
        supabase.claim_location_processing,
        payload.location_id,
        payload.request_id,
        cooldown_seconds=COOLDOWN_SECONDS,
        claim_stale_after_seconds=CLAIM_STALE_AFTER_SECONDS,
    )
    if not claimed:
        logger.info(
            "location processing cooldown skipped (location_id=%s request_id=%s)",
            payload.location_id,
            payload.request_id,
        )
        return LocationProcessingAdmissionResult(False, payload.request_id)

    logger.info(
        "location processing claim_granted "
        "(location_id=%s request_id=%s)",
        payload.location_id,
        payload.request_id,
    )

    try:
        await dispatch(payload)
    except Exception:
        await asyncio.to_thread(
            supabase.release_location_processing_claim,
            payload.location_id,
            payload.request_id,
        )
        logger.exception(
            "location processing claim released after dispatch failure "
            "(location_id=%s request_id=%s)",
            payload.location_id,
            payload.request_id,
        )
        raise

    logger.info(
        "location processing publish_succeeded "
        "(location_id=%s request_id=%s)",
        payload.location_id,
        payload.request_id,
    )

    try:
        completed = await asyncio.to_thread(
            supabase.complete_location_processing_queue,
            payload.location_id,
            payload.request_id,
        )
        logger.info(
            "location processing tracker_completed=%s "
            "(location_id=%s request_id=%s)",
            completed,
            payload.location_id,
            payload.request_id,
        )
    except Exception:
        logger.exception(
            "location processing was queued but tracker completion failed; "
            "worker will repair it (location_id=%s request_id=%s)",
            payload.location_id,
            payload.request_id,
        )

    return LocationProcessingAdmissionResult(True, payload.request_id)
```

- [ ] **Step 4: Add failing Supabase wrapper tests**

Create `tests/test_location_processing_supabase.py` with a fake RPC builder and assert exact payloads:

```python
from types import SimpleNamespace

from pinit.integrations.supabase import SupabaseService


class _Rpc:
    def __init__(self, data=True):
        self.data = data

    def execute(self):
        return SimpleNamespace(data=self.data)


class _Client:
    def __init__(self):
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return _Rpc()


def _service():
    service = object.__new__(SupabaseService)
    service.client = _Client()
    return service


def test_claim_location_processing_serializes_cooldown_and_lease() -> None:
    service = _service()
    assert service.claim_location_processing(
        42, "request-42", cooldown_seconds=2592000,
        claim_stale_after_seconds=300,
    )
    assert service.client.calls == [("claim_location_processing", {
        "p_location_id": 42,
        "p_request_id": "request-42",
        "p_cooldown_seconds": 2592000,
        "p_claim_stale_after_seconds": 300,
    })]


def test_complete_and_release_use_owned_request_id() -> None:
    service = _service()
    assert service.complete_location_processing_queue(42, "request-42")
    assert service.release_location_processing_claim(42, "request-42")
    assert service.client.calls == [
        ("complete_location_processing_queue", {
            "p_location_id": 42, "p_request_id": "request-42",
        }),
        ("release_location_processing_claim", {
            "p_location_id": 42, "p_request_id": "request-42",
        }),
    ]
```

- [ ] **Step 5: Run the wrapper tests and verify RED**

Run: `pytest -q tests/test_location_processing_supabase.py`

Expected: FAIL because the three methods are absent.

- [ ] **Step 6: Add the three SupabaseService RPC wrappers**

Add beside the existing vibe claim methods in `src/pinit/integrations/supabase.py`:

```python
def claim_location_processing(
    self,
    location_id: int,
    request_id: str,
    *,
    cooldown_seconds: int = 30 * 24 * 60 * 60,
    claim_stale_after_seconds: int = 5 * 60,
) -> bool:
    response = self.client.rpc("claim_location_processing", {
        "p_location_id": location_id,
        "p_request_id": request_id,
        "p_cooldown_seconds": cooldown_seconds,
        "p_claim_stale_after_seconds": claim_stale_after_seconds,
    }).execute()
    return bool(response.data)

def complete_location_processing_queue(
    self, location_id: int, request_id: str
) -> bool:
    response = self.client.rpc("complete_location_processing_queue", {
        "p_location_id": location_id,
        "p_request_id": request_id,
    }).execute()
    return bool(response.data)

def release_location_processing_claim(
    self, location_id: int, request_id: str
) -> bool:
    response = self.client.rpc("release_location_processing_claim", {
        "p_location_id": location_id,
        "p_request_id": request_id,
    }).execute()
    return bool(response.data)
```

- [ ] **Step 7: Run Task 2 tests and verify GREEN**

Run:

```bash
pytest -q tests/test_location_processing_admission.py tests/test_location_processing_supabase.py
```

Expected: all tests pass.

- [ ] **Step 8: Commit the backend admission unit**

```bash
git add src/pinit/api/services/location_processing_admission.py src/pinit/integrations/supabase.py tests/test_location_processing_admission.py tests/test_location_processing_supabase.py
git commit -m "feat: admit location processing once per cooldown"
```

---

### Task 3: Gate API entry points and repair the tracker in the worker

**Files:**
- Modify: `src/pinit/api/routers/proximal.py:1653-1805`
- Modify: `src/pinit/api/services/location_tasks.py:81-99`
- Modify: `tests/test_api_endpoints.py:130-210,442-526`
- Modify: `tests/test_location_tasks.py:1-90`

- [ ] **Step 1: Extend the API fake and write a cooldown rejection test**

Add claim state to `_FakeSupabase` in `tests/test_api_endpoints.py`:

```python
def __init__(self) -> None:
    self.location_processing_claimed = True
    self.location_processing_complete_calls = []
    self.location_processing_release_calls = []

def claim_location_processing(self, location_id, request_id, **kwargs):
    return self.location_processing_claimed

def complete_location_processing_queue(self, location_id, request_id):
    self.location_processing_complete_calls.append((location_id, request_id))
    return True

def release_location_processing_claim(self, location_id, request_id):
    self.location_processing_release_calls.append((location_id, request_id))
    return True
```

If `_FakeSupabase` already gains an initializer during execution, merge these fields into it rather than defining a second initializer.

Add:

```python
def test_locations_process_returns_not_queued_during_cooldown(self) -> None:
    self.supabase.location_processing_claimed = False
    _FakeDispatcher.dispatched = []
    with (
        patch.object(proximal, "get_supabase_service", return_value=self.supabase),
        patch.object(
            proximal,
            "get_pubsub_config",
            return_value=SimpleNamespace(enabled=False, project_id="", topic=""),
        ),
        patch.object(proximal, "InProcessDispatcher", _FakeDispatcher),
    ):
        response = self.client.post(
            "/locations/process",
            json={"location_id": 3001, "source": "expanded-card-open"},
        )

    assert response.status_code == 202
    assert response.json()["queued"] is False
    assert _FakeDispatcher.dispatched == []
```

- [ ] **Step 2: Run the new API test and verify RED**

Run: `pytest -q tests/test_api_endpoints.py -k 'locations_process'`

Expected: FAIL because the endpoint dispatches regardless of the claim result.

- [ ] **Step 3: Route `/locations/process` through admission**

Import `admit_location_processing`, then replace the direct dispatch with:

```python
admission = await admit_location_processing(
    payload,
    supabase=supabase,
    dispatch=_dispatch_location_processing,
)
return ProcessLocationResponse(
    queued=admission.queued,
    location_id=request.location_id,
    request_id=request_id,
)
```

Keep the canonical row existence check and Google Place ID fallback unchanged.

- [ ] **Step 4: Write tests for `/locations/add` async and synchronous cooldown behavior**

Add one test with `process_synchronously=false` and one with `true`. In both, set `location_processing_claimed = False`, assert no `_FakeDispatcher` task, and assert no direct `process_location_task` call. Pin response messages to:

```python
"Location exists; processing skipped by 30-day cooldown"
```

For the accepted synchronous test, patch `process_location_task` with `AsyncMock`, leave the claim true, and assert it is awaited once and tracker completion is recorded once.

- [ ] **Step 5: Run the add tests and verify RED**

Run: `pytest -q tests/test_api_endpoints.py -k 'locations_add'`

Expected: the new cooldown tests fail because add still dispatches/processes directly.

- [ ] **Step 6: Route both add modes through the same admission operation**

For synchronous mode, pass a local async dispatcher:

```python
async def _process_now(payload: ProcessLocationPayload) -> None:
    await process_location_task(payload)

dispatch = _process_now if process_synchronously else _dispatch_location_processing
admission = await admit_location_processing(
    processing_payload,
    supabase=supabase,
    dispatch=dispatch,
)
```

Only fetch the updated location and report `"Location processed synchronously"` when `process_synchronously and admission.queued`. For denied claims, return the existing location response with the cooldown message. For accepted async claims, retain the existing new/existing queued messages.

- [ ] **Step 7: Write the failing worker repair test**

In `tests/test_location_tasks.py`, patch `get_supabase_service` with a fake that records `complete_location_processing_queue`. Call `handle_location_task` with a `ProcessLocationPayload` while patching `process_location_task` with `AsyncMock`.

```python
assert fake_supabase.complete_calls == [(42, "request-42")]
process.assert_awaited_once()
```

- [ ] **Step 8: Run the worker repair test and verify RED**

Run: `pytest -q tests/test_location_tasks.py -k 'process_location'`

Expected: FAIL because worker receipt does not complete the tracker.

- [ ] **Step 9: Complete the tracker at worker receipt**

Add this helper to `location_tasks.py`:

```python
async def _repair_location_processing_tracker(
    payload: ProcessLocationPayload,
) -> None:
    try:
        completed = await asyncio.to_thread(
            get_supabase_service().complete_location_processing_queue,
            payload.location_id,
            payload.request_id,
        )
        logger.info(
            "process_location tracker completion=%s "
            "(location_id=%s request_id=%s)",
            completed,
            payload.location_id,
            payload.request_id,
        )
    except Exception:
        logger.exception(
            "process_location tracker repair failed "
            "(location_id=%s request_id=%s)",
            payload.location_id,
            payload.request_id,
        )
```

Call it in `handle_location_task` immediately before `process_location_task(payload)`. Do not place it inside `process_location_task`, because direct synchronous processing must record the tracker only after successful completion.

- [ ] **Step 10: Run the focused backend suite and verify GREEN**

Run:

```bash
pytest -q tests/test_location_processing_migration.py tests/test_location_processing_supabase.py tests/test_location_processing_admission.py tests/test_api_endpoints.py tests/test_location_tasks.py tests/test_location_processing.py
```

Expected: all selected tests pass.

- [ ] **Step 11: Commit API and worker gating**

```bash
git add src/pinit/api/routers/proximal.py src/pinit/api/services/location_tasks.py tests/test_api_endpoints.py tests/test_location_tasks.py
git commit -m "feat: enforce location processing cooldown"
```

---

### Task 4: Add Flutter fresh-row eligibility and trigger logic

**Files:**
- Create: `lib/services/location_processing_trigger.dart`
- Create: `test/services/location_processing_trigger_test.dart`

- [ ] **Step 1: Write failing eligibility tests**

Create a complete row factory with nonblank story, emoji, terminal image state, cuisine, dietary/vibe vectors, Google identifiers, and reviews. Add these cases:

```dart
test('incomplete never-queued row requests processing', () async {
  final row = completeRow()..['generated_summary'] = ' ';
  expect(LocationProcessingSnapshot.fromRow(row).shouldProcess(now), isTrue);
});

test('fresh tracker blocks missing fields', () async {
  final row = completeRow()
    ..['generated_summary'] = null
    ..['location_processing_queued_at'] = now.subtract(const Duration(days: 1)).toIso8601String();
  expect(LocationProcessingSnapshot.fromRow(row).shouldProcess(now), isFalse);
});

test('complete row with empty tracker does not request processing', () async {
  expect(LocationProcessingSnapshot.fromRow(completeRow()).shouldProcess(now), isFalse);
});

test('thirty-day-old tracker requests refresh for complete row', () async {
  final row = completeRow()
    ..['location_processing_queued_at'] = now.subtract(const Duration(days: 30)).toIso8601String();
  expect(LocationProcessingSnapshot.fromRow(row).shouldProcess(now), isTrue);
});

test('legacy JSON-string reviews count as present', () async {
  final row = completeRow()..['reviews'] = '[{"rating":5}]';
  expect(LocationProcessingSnapshot.fromRow(row).hasMajorGaps, isFalse);
});
```

- [ ] **Step 2: Run eligibility tests and verify RED**

Run:

```bash
cd /Users/sriharshavitta/Projects/login
flutter test test/services/location_processing_trigger_test.dart
```

Expected: compilation FAIL because the service does not exist.

- [ ] **Step 3: Implement snapshot parsing and the pure predicate**

Create `lib/services/location_processing_trigger.dart` with:

```dart
import 'dart:convert';

import 'package:login/services/recommendations_api.dart';
import 'package:login/supabase/supabase_client.dart';

const locationProcessingCooldown = Duration(days: 30);

bool _present(Object? value) => value?.toString().trim().isNotEmpty == true;

bool _hasItems(Object? value) {
  if (value is List) return value.isNotEmpty;
  if (value is String && value.trim().isNotEmpty) {
    try {
      final decoded = jsonDecode(value);
      return decoded is List && decoded.isNotEmpty;
    } catch (_) {
      return false;
    }
  }
  return false;
}

class LocationProcessingSnapshot {
  const LocationProcessingSnapshot({
    required this.locationId,
    required this.googlePlaceId,
    required this.queuedAt,
    required this.hasMajorGaps,
  });

  final int locationId;
  final String? googlePlaceId;
  final DateTime? queuedAt;
  final bool hasMajorGaps;

  factory LocationProcessingSnapshot.fromRow(Map<String, dynamic> row) {
    final queuedAt = DateTime.tryParse(
      row['location_processing_queued_at']?.toString() ?? '',
    )?.toUtc();
    final photoTerminal = row['image_stored'] == true || row['image_unavailable'] == true;
    final hasMajorGaps =
        !_present(row['generated_summary']) ||
        !_present(row['emoji']) ||
        !photoTerminal ||
        !_present(row['cuisine_primary']) ||
        !_hasItems(row['dietary_requirement_vector']) ||
        !_hasItems(row['vibe_vector']) ||
        row['updated_vibe'] != true ||
        !_present(row['google_place_id']) ||
        !_present(row['google_maps_uri']) ||
        !_hasItems(row['reviews']);
    return LocationProcessingSnapshot(
      locationId: (row['location_id'] as num).toInt(),
      googlePlaceId: row['google_place_id']?.toString().trim(),
      queuedAt: queuedAt,
      hasMajorGaps: hasMajorGaps,
    );
  }

  bool shouldProcess(DateTime now) {
    final lastQueued = queuedAt;
    if (lastQueued == null) return hasMajorGaps;
    return !now.toUtc().isBefore(lastQueued.add(locationProcessingCooldown));
  }
}
```

Use the existing `SupabaseClientManager` from `lib/supabase/supabase_client.dart`; do not create a second Supabase client singleton.

- [ ] **Step 4: Write failing trigger orchestration tests**

Inject a row loader, processing requester, and clock. Assert:

```dart
test('eligible snapshot sends one canonical request', () async {
  final requests = <int>[];
  final trigger = LocationProcessingTrigger(
    loadRow: (_) async => incompleteRow(),
    requestProcessing: ({required locationId, googlePlaceId}) async {
      requests.add(locationId);
    },
    now: () => now,
  );
  expect(await trigger.onExpanded(42), isTrue);
  expect(requests, [42]);
});

test('fresh cooldown performs no API request', () async {
  var requested = false;
  final trigger = LocationProcessingTrigger(
    loadRow: (_) async => freshIncompleteRow(),
    requestProcessing: ({required locationId, googlePlaceId}) async {
      requested = true;
    },
    now: () => now,
  );
  expect(await trigger.onExpanded(42), isFalse);
  expect(requested, isFalse);
});
```

- [ ] **Step 5: Run trigger tests and verify RED**

Run: `flutter test test/services/location_processing_trigger_test.dart`

Expected: FAIL because `LocationProcessingTrigger` is absent.

- [ ] **Step 6: Implement the fresh Supabase fetch and request orchestration**

The implementation must select only:

```text
location_id,google_place_id,location_processing_queued_at,generated_summary,emoji,image_stored,image_unavailable,cuisine_primary,dietary_requirement_vector,vibe_vector,updated_vibe,google_maps_uri,reviews
```

Add injectable typedefs and defaults:

```dart
typedef LocationProcessingRowLoader = Future<Map<String, dynamic>?> Function(int locationId);
typedef LocationProcessingRequester = Future<void> Function({
  required int locationId,
  String? googlePlaceId,
});

class LocationProcessingTrigger {
  LocationProcessingTrigger({
    LocationProcessingRowLoader? loadRow,
    LocationProcessingRequester? requestProcessing,
    DateTime Function()? now,
  })  : _loadRow = loadRow ?? _loadCanonicalRow,
        _requestProcessing = requestProcessing ?? _requestCanonicalProcessing,
        _now = now ?? DateTime.now;

  final LocationProcessingRowLoader _loadRow;
  final LocationProcessingRequester _requestProcessing;
  final DateTime Function() _now;

  Future<bool> onExpanded(int locationId) async {
    if (locationId <= 0) return false;
    final row = await _loadRow(locationId);
    if (row == null) return false;
    final snapshot = LocationProcessingSnapshot.fromRow(row);
    if (!snapshot.shouldProcess(_now())) return false;
    await _requestProcessing(
      locationId: snapshot.locationId,
      googlePlaceId: snapshot.googlePlaceId,
    );
    return true;
  }

  static Future<Map<String, dynamic>?> _loadCanonicalRow(int locationId) async {
    final row = await SupabaseClientManager().client
        .from('locations')
        .select('location_id,google_place_id,location_processing_queued_at,generated_summary,emoji,image_stored,image_unavailable,cuisine_primary,dietary_requirement_vector,vibe_vector,updated_vibe,google_maps_uri,reviews')
        .eq('location_id', locationId)
        .maybeSingle();
    return row;
  }

  static Future<void> _requestCanonicalProcessing({
    required int locationId,
    String? googlePlaceId,
  }) => RecommendationsApi().processLocation(
        locationId: locationId,
        googlePlaceId: googlePlaceId,
        source: 'expanded-card-open',
      );
}
```

- [ ] **Step 7: Run and analyze the new service**

Run:

```bash
dart format lib/services/location_processing_trigger.dart test/services/location_processing_trigger_test.dart
flutter test test/services/location_processing_trigger_test.dart
flutter analyze lib/services/location_processing_trigger.dart
```

Expected: tests pass and analyzer reports no issues.

- [ ] **Step 8: Commit the Flutter eligibility service**

```bash
git add lib/services/location_processing_trigger.dart test/services/location_processing_trigger_test.dart
git commit -m "feat: assess location processing eligibility on open"
```

---

### Task 5: Wire the expanded card without changing the UI

**Files:**
- Modify: `lib/widgets/home/expanded_location_card.dart:115-193,630-644`
- Modify: `test/widgets/home/expanded_location_card_config_test.dart`

- [ ] **Step 1: Write a failing constructor seam test**

Expose an optional `LocationProcessingTrigger? locationProcessingTrigger` on `ExpandedLocationCard`, then first write:

```dart
test('accepts an injected location processing trigger', () {
  final trigger = LocationProcessingTrigger(
    loadRow: (_) async => null,
    requestProcessing: ({required locationId, googlePlaceId}) async {},
  );
  final card = ExpandedLocationCard(
    location: LocationModel(
      locationId: 42,
      name: 'Noodle Yard',
      createdAt: DateTime.utc(2026, 7, 14),
    ),
    onClose: _noop,
    locationProcessingTrigger: trigger,
  );
  expect(card.locationProcessingTrigger, same(trigger));
});
```

- [ ] **Step 2: Run the config test and verify RED**

Run: `flutter test test/widgets/home/expanded_location_card_config_test.dart`

Expected: compilation FAIL because the parameter/property is absent.

- [ ] **Step 3: Replace unconditional processing with the trigger**

Add the optional constructor property. In state initialization use:

```dart
late final LocationProcessingTrigger _locationProcessingTrigger;

@override
void initState() {
  super.initState();
  _locationProcessingTrigger =
      widget.locationProcessingTrigger ?? LocationProcessingTrigger();
  // existing initialization remains in its current order
  unawaited(_requestLocationProcessing());
}
```

Replace the old direct API method body with:

```dart
Future<void> _requestLocationProcessing() async {
  try {
    await _locationProcessingTrigger.onExpanded(widget.location.locationId);
  } catch (error) {
    if (kDebugMode) {
      print('[ExpandedCard] Failed to assess location processing: $error');
    }
  }
}
```

Remove the now-unused `RecommendationsApi` field/import. Do not add loading state, visible controls, animation changes, or awaited work in `initState`.

- [ ] **Step 4: Run focused Flutter tests and analyzer**

Run:

```bash
dart format lib/widgets/home/expanded_location_card.dart test/widgets/home/expanded_location_card_config_test.dart
flutter test test/services/location_processing_trigger_test.dart test/services/recommendations_api_test.dart test/widgets/home/expanded_location_card_config_test.dart test/widgets/home/expanded_card/helpers/google_maps_target_test.dart
flutter analyze lib/widgets/home/expanded_location_card.dart lib/services/location_processing_trigger.dart lib/services/recommendations_api.dart
```

Expected: all tests pass and analyzer reports no issues.

- [ ] **Step 5: Commit expanded-card gating**

```bash
git add lib/widgets/home/expanded_location_card.dart test/widgets/home/expanded_location_card_config_test.dart
git commit -m "feat: gate expanded location processing"
```

---

### Task 6: Verify, migrate, deploy, and smoke-test production

**Files:**
- Verify all files changed in Tasks 1-5.
- No global backfill or permanent Cloud Run CPU configuration.

- [ ] **Step 1: Run complete changed-surface backend verification**

Because the repository's test stubs can mask installed pandas, preload real pandas as used by the previous deployment verification:

```bash
cd /Users/sriharshavitta/Projects/pinit-recommendations
python - <<'PY'
import pandas  # noqa: F401
import pytest
raise SystemExit(pytest.main([
    '-q',
    'tests/test_location_processing_migration.py',
    'tests/test_location_processing_supabase.py',
    'tests/test_location_processing_admission.py',
    'tests/test_api_endpoints.py',
    'tests/test_location_tasks.py',
    'tests/test_location_processing.py',
    'tests/test_location_completeness.py',
    'tests/test_pubsub_deployment_scripts.py',
]))
PY
python -m compileall -q src/pinit/api src/pinit/worker src/pinit/integrations
bash -n deploy.sh pubsub.sh verify_pubsub.sh
git diff --check
```

Expected: focused tests, compilation, shell syntax, and diff check all pass. Report the existing unrelated whole-suite collection issue separately if it still exists.

- [ ] **Step 2: Run complete changed-surface Flutter verification**

```bash
cd /Users/sriharshavitta/Projects/login
flutter test test/services/location_processing_trigger_test.dart test/services/recommendations_api_test.dart test/widgets/home/expanded_location_card_config_test.dart test/widgets/home/expanded_card/helpers/google_maps_target_test.dart
flutter analyze lib/services/location_processing_trigger.dart lib/services/recommendations_api.dart lib/widgets/home/expanded_location_card.dart
git diff --check
```

Expected: all focused tests pass and analyzer reports no issues.

- [ ] **Step 3: Inspect pending migration and current advisors**

Run help first rather than guessing current CLI flags:

```bash
cd /Users/sriharshavitta/Projects/pinit-recommendations
supabase db push --help
supabase migration list --local
```

Use Supabase MCP `get_advisors` for both `security` and `performance`. Fix only findings introduced by this migration; document unrelated pre-existing advisories.

- [ ] **Step 4: Apply the migration to project `umjoqvsfqhirysdjxnaf`**

Use the linked CLI project's supported `supabase db push` command shown by `--help`, or the authenticated Supabase migration workflow if the checkout is not linked. Do not run any data backfill.

Immediately verify with SQL:

```sql
select column_name, data_type
from information_schema.columns
where table_schema = 'public'
  and table_name = 'locations'
  and column_name in (
    'location_processing_queued_at',
    'location_processing_claim_id',
    'location_processing_claimed_at'
  )
order by column_name;

select routine_name, security_type
from information_schema.routines
where routine_schema = 'public'
  and routine_name in (
    'claim_location_processing',
    'complete_location_processing_queue',
    'release_location_processing_claim'
  )
order by routine_name;
```

Expected: three nullable columns and three `INVOKER` functions.

- [ ] **Step 5: Deploy the backend with Pub/Sub and request-based CPU**

```bash
cd /Users/sriharshavitta/Projects/pinit-recommendations
DEPLOY_PUBSUB=true ./deploy.sh
./verify_pubsub.sh
```

Expected for API, fast worker, and menu worker: ready, CPU throttling enabled, `minScale=0`. Do not use `--no-cpu-throttling`, always-on CPU, or positive minimum instances.

- [ ] **Step 6: Verify atomic claim behavior without retaining test state**

Choose one real location with a null tracker, then execute the following through Supabase MCP in one transaction, substituting only that numeric location ID:

```sql
begin;
select public.claim_location_processing(
  42, 'cooldown-contract-a', 2592000, 300
) as first_claim;
select public.claim_location_processing(
  42, 'cooldown-contract-b', 2592000, 300
) as concurrent_claim;
select public.release_location_processing_claim(
  42, 'cooldown-contract-a'
) as released;
rollback;
```

Expected: `first_claim=true`, `concurrent_claim=false`, and `released=true`. Replace `42` with the selected real location ID in all three calls. The rollback must leave every field unchanged.

- [ ] **Step 7: Run a controlled 30-day production smoke test**

Choose one incomplete location whose tracker is null. Record its current tracker and worker-log count, then:

1. POST `/locations/process`; assert HTTP 202 and `queued: true`.
2. Poll Supabase until `location_processing_queued_at` is non-null and both claim fields are null.
3. Confirm one `process_location` worker start for the returned request ID.
4. POST the same location again; assert HTTP 202 and `queued: false`.
5. Confirm no second worker start and the tracker timestamp did not change.

Do not alter the timestamp to simulate 30 days in production; the deterministic SQL and unit tests cover that boundary.

- [ ] **Step 8: Confirm repository state and commits**

```bash
git -C /Users/sriharshavitta/Projects/pinit-recommendations status --short --branch
git -C /Users/sriharshavitta/Projects/login status --short --branch
```

Expected: both worktrees clean on `main`. Backend is deployed from committed local code. Flutter changes require the next mobile release; do not attempt an App Store deployment without separate authorization and release tooling.
