# Location Processing Cooldown Design

## Goal

Prevent an expanded location card from admitting the same canonical location to the processing pipeline more than once in 30 days, while still allowing incomplete or stale locations to be processed when eligible.

The permanent tracker records successful Pub/Sub admission, not worker completion. Pub/Sub retries of the same admitted message remain allowed because they are part of reliable delivery rather than a new card-open admission.

## Eligibility contract

Flutter fetches a fresh, lightweight processing snapshot from the canonical Supabase `locations` row when an expanded card opens.

A location is eligible when either condition is true:

1. Its major data is incomplete and `location_processing_queued_at` is null.
2. `location_processing_queued_at` is at least 30 days old, whether or not the row currently looks complete, so time-sensitive Google data can refresh.

A non-null tracker less than 30 days old always blocks a new admission. Missing fields do not bypass the cooldown.

Major completeness covers:

- a nonblank `generated_summary` story;
- a nonblank `emoji`;
- a terminal photo result: `image_stored` or `image_unavailable`;
- a nonblank normalized cuisine;
- a nonempty dietary vector;
- a nonempty vibe vector with `updated_vibe = true`;
- a Google Place ID and canonical Google Maps URI; and
- a reviews value that contains review data in either native JSON-array or legacy JSON-string form.

Existing rows start with a null tracker. A complete existing row with no tracker does not need processing merely to populate the tracker. An incomplete existing row is admitted on its next expanded-card open and gains a tracker after Pub/Sub accepts the message.

## Flutter responsibilities

The expanded card keeps its current silent, nonblocking interaction. Opening the sheet starts an unawaited assessment without adding loading UI or delaying the entrance animation.

Flutter fetches only the fields needed by the eligibility contract for the canonical positive `location_id`. A pure eligibility function evaluates the snapshot using a UTC clock supplied by the caller for deterministic tests.

Flutter calls `POST /locations/process` only when the snapshot is eligible. A snapshot failure is logged in debug mode and does not interrupt the expanded-card experience. The backend remains authoritative, so a client-side false positive is harmless and a stale or older client cannot bypass the cooldown.

## Supabase schema and atomic admission

The `locations` table gains:

- `location_processing_queued_at timestamptz null`, the permanent 30-day tracker;
- `location_processing_claim_id text null`, an internal ownership token; and
- `location_processing_claimed_at timestamptz null`, a short-lived concurrency lease.

An index on `location_processing_queued_at` supports cooldown inspection. A partial index on `location_processing_claimed_at` supports stale-claim recovery.

Service-role-only RPCs provide the state transitions:

1. `claim_location_processing` atomically claims an eligible row only when no live claim exists. Claims older than five minutes are recoverable.
2. `complete_location_processing_queue` sets `location_processing_queued_at = now()` and clears the matching claim after Pub/Sub accepts the message.
3. `release_location_processing_claim` clears the matching claim when publishing fails before acceptance.

The functions use invoker security and revoke execution from `PUBLIC`, `anon`, and `authenticated`; only `service_role` may call them.

The short lease avoids incorrectly setting a 30-day tracker before publishing. If the API process exits between claiming and publishing, the claim expires in five minutes and the row remains eligible. There is no distributed transaction spanning Postgres and Pub/Sub, so the worker also calls the idempotent completion transition when it receives the admitted message. This repairs the tracker if publishing succeeded but the API exited before completing the database transition.

## Backend data flow

Both `/locations/process` and `/locations/add` use one admission service:

1. Generate the request ID.
2. Atomically claim the location in Supabase.
3. If the claim is denied, return without publishing. `/locations/process` reports `queued: false`; location creation/addition still returns its existing response shape.
4. Publish the canonical `process_location` message to Pub/Sub.
5. After successful publish, complete the queue transition and retain the 30-day timestamp.
6. If publish raises before acceptance, release the owned claim and propagate the error.

At worker receipt, the matching request ID performs the same idempotent completion transition before the completeness planner runs. Existing Pub/Sub ordering, retry, and dead-letter behavior remains unchanged.

The worker continues to read the full Supabase row and run only missing or stale stages. The admission tracker prevents new card opens from creating work; the completeness planner prevents Pub/Sub redelivery from repeating already persisted stages.

## Responses and observability

`ProcessLocationResponse` continues to return HTTP 202 with `queued`, `location_id`, and `request_id`. A cooldown rejection returns `queued: false` and does not publish. This remains compatible with the Flutter client, which treats every 2xx response as a completed request.

Logs distinguish `claim_granted`, `cooldown_skipped`, `publish_succeeded`, `claim_released`, and `tracker_completed`, always including `location_id` and `request_id`.

## Failure handling

- Fresh duplicate or concurrent opens: the first claim wins; the rest return `queued: false`.
- Pub/Sub publish failure: the claim is released so a later open can retry immediately.
- API exit before publish: the five-minute lease expires without changing the permanent tracker.
- API exit after publish: the worker completes the tracker when it receives the message.
- Worker failure or redelivery: Pub/Sub retries the already admitted request without creating a new admission or changing the 30-day clock.
- Flutter snapshot failure: the UI remains usable and no speculative processing request is made.

## Verification

Tests will prove:

- Flutter eligibility for incomplete/null, incomplete/fresh, complete/null, and stale tracker cases;
- defensive parsing of nullable timestamps, blank strings, vectors, and both review encodings;
- expanded-card opening calls the API only for an eligible fresh snapshot;
- the Supabase RPC contract permits one concurrent claim and rejects another;
- cooldown rejection publishes nothing;
- publish success completes the tracker;
- publish failure releases the owned claim;
- worker receipt repairs an unfinished tracker transition; and
- existing completeness planning and Pub/Sub infrastructure tests remain green.

After migration, a live SQL check will verify the new columns and RPC permissions. A controlled production smoke test will open/admit one eligible location, confirm `location_processing_queued_at` is populated, and confirm a second request returns `queued: false` without another Pub/Sub worker start.

## Non-goals

- No global backfill is run.
- No new visible Flutter UI or animation is added.
- The cooldown does not disable Pub/Sub retries of the same admitted request.
- The existing field-level completeness planner is not duplicated in the backend admission layer.
