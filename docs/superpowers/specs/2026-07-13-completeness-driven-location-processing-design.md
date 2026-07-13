# Completeness-Driven Location Processing

## Goal

Replace the source-specific, multi-task location enrichment graph with one
durable Pub/Sub message that loads the canonical Supabase row, determines the
work that is genuinely missing, and runs only those stages. Expanded-card
opens must stay fast, Google Places calls must be bounded, and a successful
Google fetch must count as complete even when Google does not offer every
optional value.

## Approved Product Rules

- Opening an expanded location card always requests processing. The Flutter
  client does not decide whether the row is complete.
- Pub/Sub remains the asynchronous delivery and retry layer.
- One ordered `process_location` message replaces the six-stage Pub/Sub task
  graph for normal location processing.
- The worker reads the complete `public.locations` row from Supabase and runs
  only missing, runnable stages.
- Google Places details, including reviews, are refreshed at most once every
  30 days after a successful fetch.
- A successful Google response sets `google_details_fetched_at` even when
  optional fields such as website, reviews, phone number, or opening hours are
  unavailable.
- Completed downstream outputs are preserved. A retry or subsequent card open
  skips their stages.
- Cloud Run remains request-based with CPU throttling and zero minimum
  instances.

## Current-State Evidence

The current behavior has two separate completeness gates:

1. The Flutter expanded card calls `/locations/add` only when `website` is
   empty.
2. The backend's existing `in-app` path checks only `updated_vibe` and does not
   run Google details, photos, emoji, menu, story, or cuisine completion for an
   existing row.

A read-only production audit on 2026-07-13 found 42,099 locations with Google
Place IDs. Among them:

- 30,420 (72.3%) have no stored Google reviews;
- 29,102 (69.1%) have no `google_maps_uri`;
- 22,875 (54.3%) have no Google `photos` payload;
- 22,709 (53.9%) have no emoji;
- 33,763 (80.2%) have no generated story summary;
- 32,025 (76.1%) have no vibe vector.

`google_details_fetched_at` already exists, but the current application code
does not read or write it. Existing timestamps range from 2026-05-07 through
2026-06-10, so they are already stale under the approved 30-day rule.

The audit also showed why raw null checking is insufficient: Google frequently
does not expose websites, review summaries, prices, phone numbers, or venue
booleans for otherwise valid places. A successful fetch marker is therefore
required to distinguish "not fetched" from "fetched and unavailable."

## Completeness Contract

Completeness applies to product-facing location data, not every nullable
database column.

### Google identity and details

The Google stage owns:

- `google_place_id`
- `name`
- `vicinity`
- `lat`, `lng`, and `geog`
- `types`
- `business_status`
- `google_maps_uri`
- `rating` and `user_ratings_total`
- `price_level`
- opening-hours fields
- `website`
- `international_phone_number`
- `photos`
- `reviews`
- `editorial_summary` and `review_summary`
- Google venue and serves booleans

The stage requires work when `google_details_fetched_at` is null or is at least
30 days old. Individual optional nulls do not reopen the stage while the fetch
marker is fresh.

When `google_place_id` is present, the worker uses the direct Places Details
endpoint. It does not run Text Search. When the Place ID is absent, the worker
may perform one exact name/address-and-coordinate Text Search to resolve the
place, store the resolved Place ID, and then request details. Ambiguous matches
must fail explicitly rather than attaching the wrong business.

The full details field mask includes reviews and the canonical Google Maps URI.
A successful response is merged non-destructively: fields present in the
response update Supabase, explicit false and zero values are retained, and
omitted optional fields do not erase previously useful values. The worker sets
`google_details_fetched_at` only after the row update succeeds.

### Photo

The photo stage is complete when either:

- `image_stored = true`; or
- `image_unavailable = true`.

If fresh Google details contain photos and neither terminal flag is set, the
existing photo pipeline runs. If Google has no photos, the stage sets
`image_unavailable` and becomes terminal until the next Google refresh makes
new photos available.

### Emoji

The emoji stage is complete when `emoji` is non-empty. It runs only when the
field is missing. A transient LLM failure remains retryable and does not write
a false completion value.

### Story, menu, cuisine, and dietary data

The content stage is complete when all of these required outputs exist:

- non-empty `generated_summary`;
- non-empty `cuisine_primary`;
- a non-empty `dietary_requirement_vector`.

When a website exists, the existing menu crawler and analysis remain the
preferred source. `menu`, `reccomended_dishes`, and rich menu data are optional
because a business may publish no accessible menu.

When no website exists, or website analysis leaves required outputs missing, a
fallback analysis uses only the canonical Supabase row: Google types,
attributes, summaries, hours, rating, address, and stored Google reviews. The
fallback fills only missing story/cuisine/dietary outputs and records low or
fallback confidence. It must not invent recommended dishes without supporting
menu or social evidence.

### Vibe

The vibe stage is complete only when:

- `vibe_vector` is a non-empty vector; and
- `updated_vibe = true`.

Vibe generation runs last so it sees fresh Google reviews, the story, cuisine,
menu analysis, and dietary data. TikTok or Instagram insight blending remains
source-aware, but it no longer bypasses missing base-location stages.

### Excluded fields

Operational locks, timestamps unrelated to source freshness, engagement
counts, user reviews, save state, and social-post metadata do not determine
base location completeness. Google reviews remain in `locations.reviews`;
`location_reviews` continues to represent Pinit/user review data.

## API and Client Contract

Add a processing endpoint for canonical locations:

```text
POST /locations/process
{
  "location_id": 17054,
  "google_place_id": "optional compatibility hint",
  "source": "in-app"
}
```

The endpoint validates the input, publishes one ordered message, and returns
an accepted response without waiting for enrichment. The message contains
`task_type=process_location`, `request_id`, `location_id`, the optional Place ID
hint, and `source`.

`POST /locations/add` remains backward compatible for Magic Search, older app
versions, and genuinely new locations. After resolving or creating the
canonical row, it publishes the same `process_location` payload instead of the
old stage graph.

The Flutter expanded card calls `/locations/process` on every open when it has
a valid canonical location ID. It no longer checks `website`. Transient Magic
Search cards continue to use `/locations/add` until they receive a canonical
location ID.

The expanded card's Maps action prefers `google_maps_uri`. Its fallback uses a
Google Maps search URL containing the location name and `query_place_id`; raw
coordinates are used only when no Place ID exists.

Processing remains asynchronous. This change guarantees that opening the card
requests completion without blocking the sheet on Google, crawling, image, or
LLM latency. Live in-sheet hydration can be layered on separately if needed;
it is not part of the processor's correctness contract.

## Processing Algorithm

The worker follows one idempotent loop:

1. Load the complete location row from Supabase.
2. Build and log a `LocationProcessingPlan` containing each missing stage and
   its reason.
3. If the Google stage is due, resolve a missing Place ID if possible, fetch
   full details including reviews, merge them into Supabase, set
   `google_details_fetched_at`, and reload the row.
4. Recalculate the plan because Google data may make later stages runnable.
5. Run photo only if its terminal flags are absent.
6. Run emoji only if it is empty.
7. Run website/menu analysis when available, then run the Supabase/Google
   fallback only for content outputs that remain missing.
8. Reload the row and run vibe only when its completion pair is missing.
9. Reload once more, calculate the final plan, and log complete, unavailable,
   retryable, and still-missing outputs.

The plan builder is a pure function over a location row plus the current time.
It is the single source of truth used by API tests, worker tests, diagnostic
logs, and any future backfill command.

## Pub/Sub Architecture

Pub/Sub remains because it keeps expanded-card requests fast and provides
durability, burst absorption, retries, ordering, and dead-letter inspection.
The simplification removes Pub/Sub as an internal workflow engine.

One filtered ordered subscription for `process_location` targets the larger
menu-capable worker. Messages use `location_id` as the ordering key. Two rapid
opens therefore run sequentially; after the first completes, the second loads
the updated row and normally no-ops.

The worker returns HTTP success only after the processor finishes. An exception
returns failure so Pub/Sub retries. A retry reruns the planner and skips stages
whose persisted outputs already completed. This lets a long first attempt make
durable progress even if a later stage fails.

The existing dead-letter topic, retry delays, maximum delivery attempts, and
inspection subscription remain. The menu worker stays private, CPU-throttled,
and configured with zero minimum instances. No background CPU or permanent
Cloud Run instance is introduced.

## Error Handling

- Missing location row: permanent error with a clear diagnostic; dead-letter
  after the configured attempts.
- Missing Place ID with an unambiguous search result: store the ID and continue.
- Missing Place ID with ambiguous/no search result: do not attach a guess;
  report the resolution failure.
- Google HTTP/transient failure: do not advance the fetch timestamp; retry.
- Google success with optional fields omitted: advance the fetch timestamp and
  continue with available data.
- Supabase write failure: fail before setting completion and retry.
- Photo absent: set `image_unavailable`; photo transport/storage failure:
  retry without setting a terminal flag.
- Website absent or menu unavailable: run the Google/review fallback.
- LLM/crawler transient failure: leave the required output missing and retry.
- A downstream failure never rolls back previously persisted successful
  stages.

## Deployment and Cutover

1. Deploy worker code that understands both legacy tasks and
   `process_location` while API publishing remains unchanged.
2. Provision the single filtered `process_location` subscription with the
   existing retry, ordering, authentication, and dead-letter policies.
3. Verify the target worker is ready, CPU-throttled, and has minimum instances
   set to zero.
4. Switch `/locations/add` and `/locations/process` to the new message.
5. Deploy the Flutter change that calls processing on every expanded-card open.
6. Confirm old subscriptions have no backlog, then disable their push delivery
   for a rollback window before removal.
7. Keep rollback limited to restoring the old API publisher; do not change
   Cloud Run billing or scaling settings.

## Testing and Verification

### Pure completeness tests

- Fresh successful Google timestamp plus optional nulls does not fetch Google.
- Timestamp exactly 30 days old does fetch Google.
- Missing timestamp fetches Google and requests reviews.
- Direct details is used when Place ID exists; Text Search is not called.
- Missing Place ID resolves by name/coordinates only when the match is
  unambiguous.
- Explicit `false` and `0` Google values are not considered missing.
- Each completed downstream stage is skipped independently.
- Vibe runs after content completion and requires both completion fields.

### Worker tests

- One message reads the full row and runs only planned stages.
- Successful Google fetch writes details and the timestamp atomically from the
  processor's perspective.
- A retry after partial success skips already-persisted stages.
- No website activates the Google/review story fallback.
- Google failure leaves the timestamp unchanged and produces a retryable worker
  response.
- A fully complete row performs one Supabase read and no external work.

### API and Flutter tests

- `/locations/process` returns promptly after publishing one message.
- `/locations/add` publishes the same message for new and existing rows.
- Every canonical expanded-card open calls processing regardless of website.
- Magic Search still canonicalizes new rows before processing.
- Maps launch prefers the canonical URI, then name plus Place ID, and only then
  coordinates.

### Production verification

- All focused tests pass before deployment.
- API and worker services remain request-based with zero minimum instances.
- A complete fixture produces a publish, consume, and no-op acknowledgement.
- An incomplete fixture fetches Google reviews, fills only missing stages, and
  completes on a second planner pass.
- Reopening that fixture produces no Google call within 30 days.
- A simulated optional Google null still records successful completion.
- Source subscription backlog remains stable and the dead-letter queue remains
  empty after smoke tests.

## Non-Goals

- Processing every production location in a one-time global backfill.
- Treating every nullable schema column as required.
- Blocking expanded-card presentation until enrichment finishes.
- Keeping the six-stage task graph for new work.
- Introducing always-on CPU, minimum instances, Cloud Run jobs, or worker
  pools.
