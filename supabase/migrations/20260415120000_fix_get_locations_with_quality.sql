-- ============================================================================
-- FIX: get_locations_with_quality — slim RPC + precomputed quality_score
-- ============================================================================
-- Problem: the live RPC returned ~45 columns (including geography(geog),
-- jsonb photos / opening_hours_periods, 15 boolean feature flags, long
-- editorial/review text). EXPLAIN ANALYZE showed execution at ~200 ms but
-- PostgREST serialisation of wide rows * 2000 rows blew past the Supabase
-- statement timeout before the client saw any bytes. It also computed
-- quality_score inline via an 18-arg compute_quality_score() call, which
-- failed overload resolution (numeric → integer is not an implicit cast)
-- and added per-row plpgsql work on top of the egress bloat.
--
-- This rewrite:
--   1. Returns ONLY the 17 columns the Python ranking layer + Flutter
--      client actually consume. Nothing else. Every dropped column is
--      pure egress.
--   2. Reads quality_score from public.location_popularity_app, which is
--      refreshed nightly by the 3am cron. No inline computation.
--   3. Uses the PostGIS `<->` KNN operator against idx_locations_geog so
--      the GIST index provides both the bbox prefilter and the ordering.
--
-- Dropped columns (vs the previous wide version), all unused downstream:
--   geog, google_place_id*, business_status, editorial_summary, website,
--   international_phone_number, types, opening_hours_text,
--   opening_hours_periods, open_now, saved_count, outdoor_seating,
--   live_music, serves_cocktails, serves_brunch, serves_wine, serves_beer,
--   good_for_groups, good_for_children, serves_vegetarian_food,
--   serves_breakfast, serves_lunch, serves_dinner, serves_coffee,
--   serves_dessert, good_for_watching_sports, photo_reference,
--   photo_reference_score, created_at, updated_at.
--
-- (* google_place_id is kept — the client uses it for Google Maps deep
--   links. emoji too, for card rendering.)
--
-- Kept:
--   location_id, google_place_id, name, vicinity, cuisine_primary,
--   rating, user_ratings_total, price_level, lat, lng, emoji,
--   vibe_vector, dietary_requirement_vector,
--   image_stored, image_unavailable, photos, extra_photos_stored,
--   distance_km, quality_score.
-- ============================================================================

DROP FUNCTION IF EXISTS public.get_locations_with_quality(
  double precision, double precision, double precision, integer
);

CREATE OR REPLACE FUNCTION public.get_locations_with_quality(
  center_lat      double precision,
  center_lng      double precision,
  radius_meters   double precision,
  result_limit    integer DEFAULT 1000
)
RETURNS TABLE(
  location_id                 bigint,
  google_place_id             text,
  name                        text,
  vicinity                    text,
  cuisine_primary             text,
  rating                      real,
  user_ratings_total          numeric,
  price_level                 numeric,
  lat                         double precision,
  lng                         double precision,
  emoji                       text,
  vibe_vector                 real[],
  dietary_requirement_vector  integer[],
  image_stored                boolean,
  image_unavailable           boolean,
  extra_photos_stored         smallint,
  distance_km                 double precision,
  quality_score               double precision
)
LANGUAGE sql
STABLE
AS $function$
  WITH center AS (
    SELECT ST_SetSRID(ST_MakePoint(center_lng, center_lat), 4326)::geography AS g
  )
  SELECT
    l.location_id,
    l.google_place_id,
    l.name,
    l.vicinity,
    l.cuisine_primary,
    l.rating,
    l.user_ratings_total,
    l.price_level,
    l.lat::double precision,
    l.lng::double precision,
    l.emoji,
    l.vibe_vector,
    l.dietary_requirement_vector,
    l.image_stored,
    l.image_unavailable,
    -- NOTE: `photos` jsonb is intentionally *not* returned here. It's the
    -- dominant source of egress (~95% of payload in the wide version) and
    -- the bulk ranking path doesn't need it. Photo resource names are
    -- fetched via a separate endpoint when the client actually needs them
    -- (expanded-card gallery / image_stored=false card). The three boolean
    -- flags below are enough for the client's cache-hit contract:
    --   * image_stored     — primary in bucket, build URL directly
    --   * image_unavailable — negative cache, stop retrying
    --   * extra_photos_stored — count of {id}_{i}.jpg already uploaded
    l.extra_photos_stored,
    (ST_Distance(l.geog, c.g) / 1000.0)::double precision AS distance_km,
    COALESCE(lpa.quality_score, 0.0)::double precision   AS quality_score
  FROM public.locations l
  CROSS JOIN center c
  LEFT JOIN public.location_popularity_app lpa
    ON lpa.location_id = l.location_id
  WHERE l.geog IS NOT NULL
    AND ST_DWithin(l.geog, c.g, radius_meters)
  ORDER BY l.geog <-> c.g
  LIMIT result_limit;
$function$;

GRANT EXECUTE ON FUNCTION public.get_locations_with_quality(
  double precision, double precision, double precision, integer
) TO authenticated, anon, service_role;
