-- ============================================================================
-- V7: SLIM get_locations_with_quality + KNN ORDERING
-- ============================================================================
-- The v6 RPC is correct but too slow — it exceeds the Supabase statement
-- timeout (~8s) because it:
--   1. Returns ~35 columns including JSONB and array fields the Python side
--      immediately throws away
--   2. Orders by a computed distance expression, which forces Postgres to
--      compute ST_Distance for every row in the radius and then sort them
--      all instead of using the GIST index for ordering
--   3. Defaults to a 7000-row limit that's much larger than what Python ever
--      actually scores
--
-- This migration replaces it with a slim version that:
--   1. Only returns the 13 columns Python actually caches
--   2. Uses the PostGIS `<->` KNN operator, which *can* use the GIST index
--      directly for nearest-neighbour ordering (O(log n) instead of O(n log n))
--   3. Defaults to result_limit = 2000
-- ============================================================================

DROP FUNCTION IF EXISTS get_locations_with_quality(
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, INT
);

CREATE OR REPLACE FUNCTION get_locations_with_quality(
  center_lat DOUBLE PRECISION,
  center_lng DOUBLE PRECISION,
  radius_meters DOUBLE PRECISION,
  result_limit INT DEFAULT 2000
)
RETURNS TABLE(
  location_id BIGINT,
  name TEXT,
  vicinity TEXT,
  cuisine_primary TEXT,
  rating REAL,
  user_ratings_total NUMERIC,
  price_level NUMERIC,
  lat NUMERIC,
  lng NUMERIC,
  vibe_vector REAL[],
  dietary_requirement_vector INT[],
  distance_km FLOAT,
  quality_score FLOAT
)
LANGUAGE sql
STABLE
AS $$
  WITH center AS (
    SELECT ST_SetSRID(ST_MakePoint(center_lng, center_lat), 4326)::geography AS g
  )
  SELECT
    l.location_id,
    l.name,
    l.vicinity,
    l.cuisine_primary,
    l.rating,
    l.user_ratings_total,
    l.price_level,
    l.lat,
    l.lng,
    l.vibe_vector,
    l.dietary_requirement_vector,
    (ST_Distance(l.geog, c.g) / 1000.0)::FLOAT AS distance_km,
    COALESCE(lpa.quality_score, 0.0)::FLOAT        AS quality_score
  FROM locations l
  CROSS JOIN center c
  LEFT JOIN location_popularity_app lpa ON lpa.location_id = l.location_id
  WHERE l.geog IS NOT NULL
    AND ST_DWithin(l.geog, c.g, radius_meters)
  ORDER BY l.geog <-> c.g
  LIMIT result_limit;
$$;

GRANT EXECUTE ON FUNCTION get_locations_with_quality(
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, INT
) TO authenticated, anon, service_role;
