## Stage 0 — Raw inventory
**`locations` table** (~Our main table). Untouched by the request itself. Two background invariants now keep it ranking-ready:
- `BEFORE INSERT/UPDATE OF lat,lng` trigger on `locations` auto-populates `geog` from coordinates ([v4_app_signal_pillars.sql:437](migrations/v4_app_signal_pillars.sql:437)).
- `AFTER UPDATE OF geog` trigger propagates to `location_popularity_app.geog` so the engagement table mirrors the source-of-truth geometry.

## Stage 1 — Cache lookup (Redis)
Request hits [proximal.py:884](src/pinit/api/routers/proximal.py:884). The cache service iterates entries on a snapped ~11 km coordinate grid and applies the **effective coverage** check:

```
distance_to_cached_centre + request_radius ≤ effective_radius_km
```

`effective_radius_km` is now the distance to the *farthest* actually-cached candidate (set at write time). In dense central London this is ~2-3 km, not the nominal 15 km. So a Notting-Hill cache no longer matches a Soho request — different cache entry.

## Stage 2A — Cache miss path: PostGIS two-pass fetch
On miss, the API calls `get_locations_with_pillars(lat, lng, 15000m, limit=1000)` ([v4_app_signal_pillars.sql:301](migrations/v4_app_signal_pillars.sql:301)). This runs **two queries** that are then unioned:

**Pass 1 — primary_set (engaged places only):**
```sql
SELECT ... FROM location_popularity_app lp
JOIN locations l USING (location_id)
WHERE ST_DWithin(lp.geog, point, 15000)   -- GIST index hit on lp.geog
ORDER BY ST_Distance(lp.geog, point)
LIMIT 1000
```
Spatial filter runs on the small denormalised `lp.geog` column with its own GIST index. Returns up to 1000 nearest places that have any app engagement signal. Each row carries pillar columns: `app_engagement_score`, `google_baseline_score`, `video_insight_score`, `share_count`, `has_app_signal=TRUE`.

**Pass 2 — fill_set (geographic top-up):**
```sql
SELECT ... FROM locations l
WHERE l.geog IS NOT NULL
  AND ST_DWithin(l.geog, point, 15000)
  AND NOT EXISTS (popularity_app row)
ORDER BY ST_Distance
LIMIT (1000 - count(primary_set))
```
Only runs if Pass 1 returned fewer than 1000. Pulls nearest unrated places to top up. `has_app_signal=FALSE`, pillar scores null/zero. Google baseline gets computed on the fly from `rating` × `log(reviews)`.

The combined union is ordered `has_app_signal DESC, distance_km ASC` — engaged places first, fill behind.

## Stage 2B — Cache write
The 1000-row payload is gzipped and stored in Redis with:
- `center_lat / center_lng` (exact, not snapped)
- `cached_radius_km = 15` (what we asked for)
- **`effective_radius_km = max(distance_km in candidates)`** (what we actually got)
- 2-hour TTL

## Stage 3 — `_rank_cached_candidates` ([proximal.py:295](src/pinit/api/routers/proximal.py:295))

Starting from the 1000 cached candidates:

**Stage 4 — Tag filter** (optional)
- `cuisine` filter: OR — keep if `cuisine_primary` or `cuisine_secondary` matches.
- `vibe` filter: AND — every requested vibe must score `vibe_vector[idx] > 50`.

**Stage 5 — User radius filter**
Per-candidate haversine vs the user's actual `radius_km` (default 2 km). The big 15 km cone collapses to the user's request. Typically 50–200 candidates remain.

**Stage 6 — Per-user score computation**
For each survivor:
- pillar reads (cache): `app_engagement_score`, `google_baseline_score`, `video_insight_score`, `share_count`, `has_app_signal`
- per-user computed: `social_score`, `collaborative_score`, `vibe_score`, `dietary_match`, `dietary_penalty`
- adaptive weights: cold-start drains collaborative→app_engagement; no-friends drains social→app+video+collab

```
blended  = w_app_engagement·app_engagement
         + w_social·social
         + w_collaborative·collaborative
         + w_video_insight·video_insight
         + w_google_baseline·google_baseline
         + w_vibe·vibe
         + w_dietary·dietary_match

final_score = blended × dietary_penalty × share_boost × fill_factor
```

Where:
- **dietary_penalty** ∈ [0.05, 1.0] — drops to 0.05 when a user with strong dim (e.g. veg=100) hits a place with low capability on that dim. Hard mismatch effectively un-recommends.
- **share_boost** ∈ [1.0, 1.6] — log of `share_count` (saves with `saved_method ∈ {tiktok, instagram}`), gated by `video_insight_score`.
- **fill_factor** = 1.0 for `has_app_signal=TRUE`, 0.6 for fill — guarantees engaged places outrank unrated geographic fill at parity.

**Stage 7 — Diversity layer** ([proximal.py:618](src/pinit/api/routers/proximal.py:618))
Composed in order:
1. **Top-vibe-tag diversification** — slot allocation across the user's top 5 vibe tags (existing behaviour).
2. **Bubble boost** — group mode only.
3. **Recently-seen decay** — multiplies `final_score` by `0.55 + 0.45·(1 - exp(-Δt/7d))`. Same query, same user, different day → different ordering.
4. **Cuisine MMR** — replaces an "all Italian" cluster with a mixed top-K while keeping per-item scores high.
5. **Seeded jitter** — opt-in, deterministic per (user, day).

**Stage 8 — Truncation**
`max_results` cut (default 20). Ranks 1..N assigned.

## Funnel snapshot (typical Soho 1.5 km request)
```
locations table                    ~500,000  global
       ↓ trigger keeps geog populated
       ↓ cache key + effective-coverage check
cache miss → SQL two-pass
       primary_set (engaged)         ≤ 1000  via lp.geog GIST
       fill_set (top-up)             ≤ 1000-N  via locations.geog GIST
       union                          1000
       ↓ Redis write (effective_radius captured)
       ↓ tag filters (optional)
                                  50–1000
       ↓ user 1.5 km radius
                                  50–200
       ↓ blend + dietary_penalty + share_boost + fill_factor
                                  50–200  (mismatched ≈ 0)
       ↓ vibe-tag diversity → bubble → seen-decay → cuisine MMR
                                    ≤ 20
                              FINAL: 20 with rank, has_app_signal, share_boost
```

## What's different vs. the old flow
| Stage | Old | New |
|---|---|---|
| Source of inventory | `get_locations_with_quality` — locations only | `get_locations_with_pillars` — engagement-first + fill |
| Spatial index | `locations.geog` GIST | `lp.geog` GIST for Pass 1 (smaller, hotter) |
| Cache hit check | configured 15 km radius | actual coverage radius |
| Quality score | one conflated number | 4 split pillars + provenance flag |
| Dietary handling | dot-product weight | match weight + multiplicative penalty |
| Social-share lift | none | post-blend `share_boost` (tiktok/instagram saves) |
| Diversity | top-vibe-tag only | + recently-seen decay + cuisine MMR |
| Fill places | absent | included with 0.6× demotion |