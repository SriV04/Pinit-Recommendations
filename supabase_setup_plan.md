# Supabase Schema Setup Plan

_Last updated: 2025-12-17_

This note captures the work needed to reshape the Supabase schema so it lines up with the recommendation system plan. It focuses on the six tables we want live in Supabase and how they connect to the existing public schema shown in the legacy DDL.

---

## 1. Current State (What Exists Today)

Relevant tables in Supabase right now:
- `locations` – canonical location inventory (Place IDs, ratings, price level, lat/lng, etc.).
- `tags` – taxonomy table with `text`, `prompt_description`, `tag_type`, `Colour`. This needs to match the Python taxonomy in `src/pinit/tag_taxonomy.py`.
- `location_tags`, `user_tags`, `user_recommendations`, `user_location_actions` – older linkage tables that we will deprecate or reshape.
- Social tables (`bubbles`, `bubble_members`, `bubble_locations`, `user_friends`) and media tables (`videos`, `location_popularity_*`) that will remain but are out of scope for this migration.

We must keep historical data, so any schema changes should be additive (create new tables / columns, backfill, then drop old artifacts once the app moves over).

---

## 2. Target Tables and Specifications

Each table lists the contract, the minimum columns we need, suggested indexes, and how it maps back to today’s schema.

### 2.1 `locations_processed`

Purpose: snapshot of enriched/cleaned location rows we want the rec engine to work off, tied back to `public.locations`.

Required columns:
- `processed_location_id uuid PRIMARY KEY DEFAULT gen_random_uuid()`
- `location_id bigint NOT NULL REFERENCES public.locations(location_id)`
- `google_place_id text NOT NULL`
- `lat numeric NOT NULL`
- `lng numeric NOT NULL`
- `data_version text NOT NULL DEFAULT 'v1'`
- `source_payload jsonb` – raw normalized JSON blob from our pipeline
- `derived_attributes jsonb` – flattened features we can hydrate into the feature store
- `ingested_at timestamptz NOT NULL DEFAULT now()`
- `valid_until timestamptz` – null when active, set when superseded

Indexes:
- `UNIQUE(location_id, data_version)` to prevent duplicate rows per version.
- `INDEX ON (google_place_id)` for lookup when ingesting new data.
- `INDEX ON (lat, lng)` (or PostGIS `GEOGRAPHY` index later) for proximity searches without joining back to `public.locations`.

DDL sketch:
```sql
CREATE TABLE public.locations_processed (
  processed_location_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  location_id bigint NOT NULL REFERENCES public.locations(location_id),
  google_place_id text NOT NULL,
  lat numeric NOT NULL,
  lng numeric NOT NULL,
  data_version text NOT NULL DEFAULT 'v1',
  source_payload jsonb NOT NULL,
  derived_attributes jsonb,
  ingested_at timestamptz NOT NULL DEFAULT now(),
  valid_until timestamptz,
  UNIQUE (location_id, data_version)
);
CREATE INDEX idx_locations_processed_place ON public.locations_processed (google_place_id);
CREATE INDEX idx_locations_processed_lat_lng ON public.locations_processed (lat, lng);
```

`derived_attributes` holds dynamic, model-specific features (e.g., embedding IDs, popularity scores, hidden-gem residuals) that would otherwise force schema migrations every time we add/remove a signal. Keeping them in JSON keeps the table narrow, lets analytics pipelines unpack only what they need, and avoids churning indexes for attributes that change frequently or are sparsely populated.

**Why keep both `locations` and `locations_processed`?**
- `locations` remains the canonical app surface: it backs all existing foreign keys (videos, user actions, bubble locations) and stays stable so Flutter and PostgREST integrations don’t break when we change the enrichment pipeline.
- `locations_processed` is a versioned cache owned by the data team. We can run heavy ingestion, experiment with new derived features, and roll back without mutating the production table or touching RLS policies. It also lets us record multiple processed versions per place (e.g., Google baseline vs. social-enriched) and choose which version feeds the recommender.
- Keeping them separate preserves provenance: if we detect regressions we can diff the JSON payloads between versions without losing the original rows in `public.locations`.

Once the enrichment pipeline fully replaces the legacy ingestion, we could collapse the two tables, but for now the dual-table setup keeps the core app stable while the ML/recommender workflows iterate quickly.

### 2.2 `tags`

The new taxonomy supersedes the legacy table. Actions:
- Align column names with Python (`color` instead of `Colour`).
- Enforce NOT NULL on `text`, `tag_type`, `prompt_description`, `color`.
- Add `slug` (lowercase text) for indexing and a unique constraint.

DDL sketch:
```sql
ALTER TABLE public.tags
  RENAME COLUMN "Colour" TO color,
  ALTER COLUMN text SET NOT NULL,
  ALTER COLUMN tag_type SET NOT NULL,
  ALTER COLUMN prompt_description SET NOT NULL,
  ALTER COLUMN color SET NOT NULL;
ALTER TABLE public.tags ADD COLUMN IF NOT EXISTS slug text;
UPDATE public.tags SET slug = lower(regexp_replace(text, '\\s+', '_', 'g')) WHERE slug IS NULL;
ALTER TABLE public.tags ADD CONSTRAINT tags_slug_key UNIQUE (slug);
```

Seed data: reuse `src/pinit/tag_taxonomy.py` to generate the CSV/insert statements (one tag per `TagDefinition` row).

### 2.3 `location_tags`

We keep the table name but clarify its model:
- `location_tag_id uuid PRIMARY KEY DEFAULT gen_random_uuid()`
- `location_id bigint NOT NULL REFERENCES public.locations(location_id)`
- `tag_id uuid NOT NULL REFERENCES public.tags(tag_id)`
- `score real NOT NULL CHECK (score BETWEEN 0 AND 1)`
- `confidence real CHECK (confidence BETWEEN 0 AND 1)`
- `source text NOT NULL DEFAULT 'model_v1'`
- `explanation text` – optional string summarizing why the tag was applied
- `updated_at timestamptz NOT NULL DEFAULT now()`

Indexes:
- `UNIQUE(location_id, tag_id, source)` to avoid duplicates when recomputing.
- Covering index on `(tag_id, score DESC)` for “top venues for tag” queries.

### 2.4 `user_tag_affinities`

New table that replaces scattered logic in `user_tags` and `user_location_actions`.

Columns:
- `user_id uuid NOT NULL REFERENCES public.users(supabase_id)`
- `tag_id uuid NOT NULL REFERENCES public.tags(tag_id)`
- `affinity real NOT NULL CHECK (affinity BETWEEN 0 AND 1)` – normalized preference score
- `evidence jsonb` – structured summary (counts, last action, etc.)
- `updated_at timestamptz NOT NULL DEFAULT now()`
- `PRIMARY KEY (user_id, tag_id)`

Indexes:
- `INDEX ON (tag_id, affinity DESC)` for matching users per tag.

### 2.5 `recommendation_runs`

Stores metadata for every batch/real-time recommendation generation job.

Columns:
- `run_id uuid PRIMARY KEY DEFAULT gen_random_uuid()`
- `user_id uuid REFERENCES public.users(supabase_id)` – nullable for reusable/global runs
- `run_type text NOT NULL CHECK (run_type IN ('user_feed','bubble_feed','adhoc','batch_explore'))`
- `is_reusable boolean NOT NULL DEFAULT false` – “both reusable” requirement
- `params jsonb NOT NULL` – filter/tag thresholds, location set, etc.
- `status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','succeeded','failed'))`
- `started_at timestamptz DEFAULT now()`
- `finished_at timestamptz`
- `notes text`

Indexes:
- `INDEX ON (user_id, started_at DESC)` – latest runs per user.
- `INDEX ON (is_reusable) WHERE is_reusable = true` – quickly list template runs.

### 2.6 `recommendation_candidates`

Child table keyed by `run_id`. We capture raw candidates before applying feed-specific logic.

Columns:
- `candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid()`
- `run_id uuid NOT NULL REFERENCES public.recommendation_runs(run_id) ON DELETE CASCADE`
- `location_id bigint NOT NULL REFERENCES public.locations(location_id)`
- `rank integer`
- `score real NOT NULL`
- `reason jsonb` – explanation (top tags, friends who liked it, etc.)
- `features jsonb` – cached feature vector used for ranking
- `is_reusable boolean NOT NULL DEFAULT false` – copies `recommendation_runs.is_reusable`; set via trigger
- `generated_at timestamptz NOT NULL DEFAULT now()`

Indexes:
- `UNIQUE (run_id, location_id)` so we don’t store duplicates.
- `INDEX ON (location_id)` for diagnostics (e.g., how often a place appears).

Trigger idea:
```sql
CREATE FUNCTION set_candidate_reusable_flag()
RETURNS trigger AS $$
BEGIN
  NEW.is_reusable := (SELECT is_reusable FROM public.recommendation_runs WHERE run_id = NEW.run_id);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER recommendation_candidates_reusable
BEFORE INSERT ON public.recommendation_candidates
FOR EACH ROW EXECUTE FUNCTION set_candidate_reusable_flag();
```

---

## 3. Migration / Implementation Steps

1. **Prep + backups**
   - Snapshot the current Supabase database (`supabase db dump`) so we can roll back.
   - Confirm `pgcrypto` extension is enabled (needed for `gen_random_uuid()`).

2. **Tags alignment**
   - Run the `ALTER TABLE public.tags ...` statements above.
   - Materialize the taxonomy from `src/pinit/tag_taxonomy.py` (export to CSV, use `supabase db push` or SQL inserts).
   - Verify there is a slug + color for every row; update Flutter to use the new `color` column name.

3. **New tables**
   - Execute the DDL for `locations_processed`, `user_tag_affinities`, `recommendation_runs`, `recommendation_candidates`.
   - Adjust `location_tags` schema in place (add columns + constraints).

4. **Backfill data**
   - `locations_processed`: run the ingestion script (or notebook) to load historical London CSV rows. Use `google_place_id` from CSV to join the `public.locations` table and insert `source_payload` JSON.
   - `location_tags`: compute tag scores per location using the pipeline and insert/upsert with the `source` set to `model_v1`.
   - `user_tag_affinities`: derive from `user_location_actions` (saved/liked counts mapped to tags) and any manual `user_tags` entries.
   - Seed at least one reusable `recommendation_runs` row (e.g., “Top Hidden Gems London v1”) and attach mock `recommendation_candidates` to ensure the cascade works.

5. **App + pipeline updates**
   - Update ingestion code to write into `locations_processed` and `location_tags`.
   - Update recommender jobs to log runs/candidates via the new tables rather than `user_recommendations`.
   - Long term: retire `user_recommendations` or repurpose it as downstream cache fed from `recommendation_candidates`.

6. **Policies and access**
   - Define Row Level Security policies for each new table so that:
     - Admin/batch service role can read/write everything.
     - End users can only read recommendations targeted at them (`recommendation_runs.user_id = auth.uid()`).
   - Add PostgREST views if the Flutter client only needs a subset of fields.

7. **Monitoring / housekeeping**
   - Create materialized views or scheduled jobs to prune old `locations_processed` versions (keep the latest N versions).
   - Add `updated_at` triggers where needed to track freshness.

---

## 4. Deliverables Checklist

- [ ] SQL migration file that creates/updates all target tables.
- [ ] Backfill scripts or notebooks stored in `output/pinit_notebook/`.
- [ ] Tag taxonomy export derived from `src/pinit/tag_taxonomy.py`.
- [ ] Documentation in the repo (this file) outlining the schema and migration steps.
- [ ] Verification queries: smoke tests that confirm row counts and foreign-key integrity across the new tables.

Once the checklist is complete we can hook the recommendation engine + ingestion jobs directly into Supabase with confidence.

---

## 5. Repository Implementation Notes

- Supabase sync tooling now lives in `src/supabase/`. Key entrypoints:
  - `supabase/schema.py` – executable SQL statements + helper to apply them via a psycopg connection (`apply_schema_statements`).
  - `supabase/sync.py` – CLI + helper functions to push pipeline CSV outputs into Supabase (`python -m supabase.sync --output-dir output/pinit --dry-run` for local validation).
  - `supabase/client.py` – lightweight PostgREST wrapper with chunked upserts.
  - `supabase/config.py` – environment-driven credential loader.
- Expected env vars: `SUPABASE_URL`, `SUPABASE_ANON_KEY` (or `SUPABASE_API_KEY`), optional `SUPABASE_SERVICE_KEY`, `SUPABASE_DB_URL`, `SUPABASE_SCHEMA`.
- Sync flow:
  1. Run the pipeline (`python -m pinit.pipeline ...`) to refresh CSVs.
  2. Export schema using `apply_schema_statements` or run the SQL statements directly.
  3. Configure Supabase creds (e.g., `export SUPABASE_URL=...`).
  4. Execute `python -m supabase.sync --output-dir output/pinit --data-version v1`.
  5. When testing changes locally, add `--dry-run` to validate payload sizes without hitting Supabase.
