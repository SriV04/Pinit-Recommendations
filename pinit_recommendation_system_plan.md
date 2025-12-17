# Pinit Recommendation System Implementation Plan (London Seed Dataset)

_Last updated: 2025-12-17_

## 1. Objective

Build a production-ready recommendation system for **food spots and hangout spots** that:
- Personalizes to **user taste** (content + behavior).
- Leverages **social signals** (friends + bubbles/group chats).
- Incorporates **social media traffic** (videos/mentions).
- **Surfaces niche / under-exposed places** and avoids “prominence feedback loops” (the Maps-style dynamic where visibility compounds into more visibility).
- Ships incrementally in Supabase + Flutter, with an MVP that does not require ML, and a clear upgrade path to embeddings and learning-to-rank.

---

## 2. Inputs (What You Already Have)

### 2.1 Extracted London CSVs

**A) `london_restaurant_details.csv`**
- place_id, name, types, rating, user_ratings_total, price_level
- lat, lon, vicinity, business_status
- editorial_summary, website, international_phone_number
- opening_hours_text, opening_hours_periods, open_now
- cuisine_detected, cuisine_source
- top_review_language, top_language_share, n_reviews_fetched, review_language_counts_json

**B) `london_restaurants_reviews.csv`**
- place_id, author_name, language, rating, relative_time_description, time, text

**C) `london_restaurants.csv`**
- place_id, name, types, rating, user_ratings_total, price_level
- lat, lon, vicinity, business_status, permanently_closed
- source_lat, source_lon, grid_id

### 2.2 Supabase Tables Available (Current Schema Highlights)

- `locations(location_id, name, vicinity, lat, lng, cuisine, rating, user_ratings_total, price_level, google_place_id, ...)`
- `tags(tag_id, text, prompt_description, tag_type, Colour)`
- `location_tags(location_id, tag_id, score)`
- `users, user_tags, user_location_actions`
- `user_friends(influence, status)`
- `bubbles, bubble_members, bubble_locations`
- `location_popularity_app`, `location_popularity_social`
- `videos(extracted_location_id)`

---

## 3. Outputs (What We Will Produce)

1) **Canonical Location Inventory** in `locations` with stable external key:
- `google_place_id = place_id` (Places ID).

2) **Tag Graph**:
- `tags`: controlled taxonomy (cuisine, vibe, occasion, dietary, schedule, etc.).
- `location_tags`: per-location tag scores (0–100).

3) **User Taste Profiles**:
- `user_tags`: per-user tag affinities derived from behavior.
- Optional (later): `user_embeddings` (pgvector).

4) **Recommendations Cache**:
- `user_recommendations`: precomputed feed per user with scores and “reason” metadata.

5) **Anti-prominence / Hidden Gems**:
- A computed “expected popularity” baseline + residual used to promote under-exposed but relevant places.

---

## 4. Database Additions (Minimal + Optional)

### 4.1 Required: `user_recommendations`

```sql
CREATE TABLE IF NOT EXISTS public.user_recommendations (
  user_id uuid NOT NULL REFERENCES public.users(supabase_id),
  location_id bigint NOT NULL REFERENCES public.locations(location_id),
  score real NOT NULL,
  rank integer NOT NULL,
  reason jsonb DEFAULT '{}'::jsonb,
  generated_at timestamp without time zone NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, location_id)
);

CREATE INDEX IF NOT EXISTS idx_user_recs_user_rank
  ON public.user_recommendations (user_id, rank);
```

**Why:** fast feed reads from Flutter; avoids heavy joins at app open.

### 4.2 Recommended: location provenance + freshness fields

```sql
ALTER TABLE public.locations
  ADD COLUMN IF NOT EXISTS source text DEFAULT 'google',
  ADD COLUMN IF NOT EXISTS first_seen timestamp without time zone DEFAULT now(),
  ADD COLUMN IF NOT EXISTS last_seen timestamp without time zone DEFAULT now(),
  ADD COLUMN IF NOT EXISTS data_confidence real DEFAULT 1.0;
```

**Why:** supports “discovered” (social/user-added) locations and freshness.

### 4.3 Optional: computed feature tables

**A) `location_features` (derived scalar features)**
```sql
CREATE TABLE IF NOT EXISTS public.location_features (
  location_id bigint PRIMARY KEY REFERENCES public.locations(location_id),
  price_bucket text,
  is_open_late boolean,
  is_breakfast boolean,
  is_brunch boolean,
  expected_popularity real,
  residual_popularity real,
  updated_at timestamp without time zone DEFAULT now()
);
```

**B) Embeddings (later) — requires `pgvector`**
```sql
-- Enable pgvector in Supabase if not enabled
-- create extension if not exists vector;

CREATE TABLE IF NOT EXISTS public.location_embeddings (
  location_id bigint PRIMARY KEY REFERENCES public.locations(location_id),
  embedding vector(384),
  updated_at timestamp without time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.user_embeddings (
  user_id uuid PRIMARY KEY REFERENCES public.users(supabase_id),
  embedding vector(384),
  updated_at timestamp without time zone DEFAULT now()
);
```

---

## 5. Ingestion Pipeline (CSV → Supabase)

### 5.1 Canonical ingestion rules

- **Identity key:** `place_id` from CSV becomes `locations.google_place_id`.
- **Upsert policy:**
  - Insert if new.
  - Update if existing (rating, user_ratings_total, price_level, phone, website, vicinity, lat/lng).
- **Filter out closures:**
  - `permanently_closed = true` OR `business_status != OPERATIONAL` ⇒ skip or mark `source='inactive'`.

### 5.2 Implementation steps

1) Load `london_restaurant_details.csv` (authoritative).
2) Upsert into `locations`:
   - name
   - vicinity
   - lat → `locations.lat`, lon → `locations.lng`
   - rating, user_ratings_total, price_level
   - cuisine (set coarse cuisine from `cuisine_detected_ext` or `cuisine_detected`)
   - google_place_id = place_id
   - last_seen = now()
3) Optionally store raw opening hours JSON into a new `locations.opening_hours_json` column (recommended if you want schedule-aware recs).

### 5.3 Practical note
Keep CSVs for provenance, but treat Supabase as the live source of truth for the app.

---

## 6. Tag Taxonomy (Netflix micro-genres + Hinge prompts)

### 6.1 Tag types (recommend `tag_type` enum values)

- `CUISINE`
- `DIETARY`
- `VIBE`
- `OCCASION`
- `DRINKS`
- `SCHEDULE`
- `VALUE`
- `CATEGORY` (restaurant/cafe/bar/takeaway)
- `AREA` (optional later)

### 6.2 Starter tag list (examples)

**Cuisine:** italian, indian, japanese, korean, thai, chinese, vietnamese, mexican, mediterranean, british, pub, bakery, cafe, seafood, steakhouse, vegan_vegetarian.

**Dietary:** vegetarian_friendly, vegan_friendly, halal_friendly, gluten_free_options.

**Vibe:** cozy, romantic, lively, quiet, trendy, casual, formal, family_friendly.

**Occasion:** date_night, brunch, quick_bite, group_hang, business_meeting, solo_friendly.

**Drinks:** cocktails, wine_bar, craft_beer.

**Schedule:** open_late, open_early, sunday_open.

**Value:** great_value, pricey.

**Category:** restaurant, cafe, bar, takeaway.

**Principle:** start with 40–80 tags. Expand only when detection is reliable.

---

## 7. Location Tagging (Using Your Sources)

We will produce `location_tags(location_id, tag_id, score)`.

### 7.1 Stage A — Deterministic tags (high precision)

From `details.csv`:
- **Cuisine**: `cuisine_detected` (score 85–95)
- **Category**: from `types` (score 70–90 depending on specificity)
- **Value/Price bucket**: from `price_level` (score 80)
- **Schedule**:
  - compute `open_late` if any close time ≥ 23:00 (score 70–85)
  - compute `open_early` if opens ≤ 08:00 (score 70–85)
  - compute `sunday_open` if Sunday has hours (score 60–75)

### 7.2 Stage B — Review-text tags (high value)

From `reviews.csv` (English first):
- Tokenize and apply curated phrase dictionaries for:
  - Vibe (cozy, romantic, lively, quiet, trendy)
  - Occasion (brunch, date night, group)
  - Dietary (vegan options, vegetarian options)
  - Drinks (cocktails, wine bar)

**Scoring approach:**
- For each tag, compute:
  - `mentions = #reviews containing keywords`
  - `unique_authors = #distinct author_name`
  - `score = min(100, 20 + 15*log1p(unique_authors) + 10*log1p(mentions))`
- Require a minimum threshold before writing a tag (e.g., `unique_authors >= 2` OR `mentions >= 3`).

### 7.3 Stage C — Embedding similarity (optional upgrade)

Build a “restaurant text”:
- name + editorial_summary + top 3 review snippets (English)
- plus normalized `types` + cuisine

Generate embeddings and store in `location_embeddings`.

Use embeddings for:
- “More like this” candidates
- user taste embedding = average of saved/liked embeddings

---

## 8. User Taste Profiles (Spotify + Netflix)

### 8.1 Behavioral signals (from `user_location_actions`)

Define action weights:
- save: 3.0
- like: 2.0
- share_to_bubble: 2.5 (if you log it)
- dismiss: -1.5 (if you log it)
- detail_view: 0.5
- impression: 0.1

Include recency decay:
- weight *= exp(-days_since / 30)

### 8.2 Derive `user_tags` from behavior

For each user:
- For each interacted location, take its `location_tags`.
- Sum into user tag affinities using action weight and recency.
- Normalize to 0–100.
- Upsert into `user_tags` (or store in a derived table if you prefer to keep `user_tags` as explicit “onboarding tags”).

**Recommendation:** keep `user_tags` for explicit onboarding + add a derived table `user_tag_affinities` for computed taste.

### 8.3 Social taste (Hinge-style “social proof”)

Compute friend-weighted signals:
- for each friend (followee), incorporate their saved locations into the user’s candidate pool
- weight by `user_friends.influence` and by closeness proxies:
  - bubbles shared
  - bubble activity
  - recency

---

## 9. Recommendation Pipeline (Multi-Stage)

### 9.1 Candidate generation (retrieve hundreds, not all)

Union of candidate sets:

1) **Taste-based** (content)
- top locations with high overlap with user’s top tags
- later: embedding nearest neighbors

2) **Friend-based**
- locations saved/liked by followees (weighted by influence)

3) **Bubble-based**
- `bubble_locations` in bubbles the user is in (especially active bubbles)

4) **Trending-based**
- `location_popularity_social` (mentions)
- `location_popularity_app` (saves/likes)

5) **Exploration pool**
- nearby / under-exposed / new locations
- ensure a minimum “novelty quota”

### 9.2 Ranking (score each candidate)

Score components (all normalized 0–1):
- `taste_score` (tag affinity / similarity)
- `friend_score`
- `bubble_score`
- `trend_score_social`
- `trend_score_app`
- `quality_score` (Bayesian-smoothed rating)
- `freshness_score`
- optional `distance_score`

**Adaptive weights** by user state:
- New user: heavier trend + quality + broad taste onboarding
- Many friends: heavier friend/bubble
- High engagement: heavier taste + exploration

### 9.3 Feed composition (Spotify/Netflix “rows” + anti-bias)

Generate a blended feed with explicit quotas per 20:
- 8 “For You” (taste-heavy)
- 5 “Friends are into”
- 4 “Trending now”
- 3 “Hidden gems” (under-exposed residual promotion)

This prevents a single popularity axis from dominating and supports discovery.

---

## 10. Anti-Prominence: “Hidden Gems” via Residuals

### 10.1 Baseline expected popularity

Compute an expected popularity based on structural features you already have:
- cuisine, price_level, types
- area proxy (vicinity tokens)
- maybe opening-hour tags

Target:
- `log1p(user_ratings_total)` (from Google data) initially
- later swap to `log1p(location_popularity_app.saves_count)` for in-app truth

Modeling options:
- MVP: linear regression / ridge on one-hot features
- Later: gradient-boosted trees

### 10.2 Residual computation
- `residual = actual - expected`
- Promote **negative residual** items that still match the user’s taste:
  - “under-exposed but relevant”

Store in `location_features.expected_popularity` and `residual_popularity`.

---

## 11. Scheduling and Runtime Architecture (Supabase + Flutter)

### 11.1 Background jobs (cron)
Run hourly:
- update popularity aggregates (app + social)
- recompute active users’ `user_recommendations`

Run daily:
- refresh residual baseline
- refresh embeddings (if used)

Implementation:
- Supabase scheduled Edge Function, or external worker calling Supabase RPC.

### 11.2 Realtime updates (optional)
Flutter subscribes to:
- `user_recommendations` filtered by user_id
When new recs are written, UI updates automatically.

### 11.3 On-demand refresh (event-driven)
When user saves/likes:
- enqueue “recompute this user” (Edge Function)
This keeps personalization feeling live without recomputing everyone.

---

## 12. API / Query Patterns

### 12.1 Feed fetch
- Query `user_recommendations` join `locations` for cards
- Order by `rank` ascending

### 12.2 Explanations
Use `reason` JSONB to render:
- “Because 3 friends saved this”
- “Trending on social this week”
- “Matches your ‘cozy’ + ‘brunch’ preferences”

---

## 13. Evaluation (Minimum viable analytics)

Log impression-level events in `user_location_actions` (or a dedicated table):
- impression, detail_view, save, like, dismiss, share_to_bubble

Track:
- save-through-rate (STR)
- diversity (tag entropy per feed)
- popularity concentration (impressions share to top X% popular locations)
- novelty rate (% unseen locations)

These metrics protect you from inadvertently recreating Maps-style allocation loops.

---

## 14. Implementation Roadmap

### Milestone A (MVP, 1–2 weeks)
- Ingest London `details.csv` into `locations` (upsert by place_id)
- Create `tags` taxonomy and deterministic `location_tags`
- Build `user_recommendations` table
- Simple recommender v1: taste tags + friend saves + trending + quotas

### Milestone B (2–4 weeks)
- Review-based tagging dictionaries + confidence scoring
- Add on-demand recompute for user after save/like
- Add diversity constraints (max cuisine share in top 20)

### Milestone C (4–8 weeks)
- Expected popularity model + residual “hidden gems” bucket
- Embeddings via pgvector (optional but high leverage)

### Milestone D (later)
- Learning-to-rank with logged impressions
- Time-of-day contextual ranking (open now, brunch hours)
- Multi-city ingestion + continuous discovery from social content

---

## 15. Appendix: Practical Implementation Notes

### A) Mapping CSV columns to Supabase `locations`

- `place_id` → `google_place_id`
- `lat` → `lat`
- `lon` → `lng`
- `rating` → `rating`
- `user_ratings_total` → `user_ratings_total`
- `price_level` → `price_level`
- `vicinity` → `vicinity`
- `cuisine_detected` → `cuisine` (coarse) + tags (fine)

### B) Tag persistence strategy

- `tags.text` = machine-friendly stable string (e.g., `date_night`, `cozy`)
- `tags.prompt_description` = user-facing explanation (“Great for dates”)
- `location_tags.score` = confidence/strength (0–100)

### C) Don’t overfit early
Start with deterministic tags + simple scoring and ship.
Add embeddings and residuals once you have enough behavior data and stable ingestion.

---

## 16. Next Deliverables (Engineering Tickets)

1) SQL migration:
- create `user_recommendations`
- add `source/first_seen/last_seen/data_confidence` to `locations`
- (optional) create `location_features`

2) Python ETL:
- ingest `details.csv` → `locations` upsert
- create tags + write `location_tags`

3) Review tagger:
- keyword dictionaries + scoring
- write additional `location_tags`

4) Recommender job:
- generate candidates + rank + write `user_recommendations`
- cron schedule + on-demand trigger
