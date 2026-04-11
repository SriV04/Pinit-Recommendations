-- ============================================================================
-- V6: FIX get_locations_with_quality COLUMN LIST
-- ============================================================================
-- The v4 RPC inherited an outdated column list from v3 that references
-- columns no longer present on `locations`. This caused every request to
-- fail with `column l.review_language_counts_json does not exist` and fall
-- back to a SELECT * egress disaster.
--
-- This migration:
--   1. Removes `review_language_counts_json` from the return shape
--   2. Matches `vibe_vector` to the actual column type (REAL[], not INT[])
--   3. Otherwise keeps the same ordered column list the Python side expects
-- ============================================================================

DROP FUNCTION IF EXISTS get_locations_with_quality(
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, INT
);

CREATE OR REPLACE FUNCTION get_locations_with_quality(
  center_lat DOUBLE PRECISION,
  center_lng DOUBLE PRECISION,
  radius_meters DOUBLE PRECISION,
  result_limit INT DEFAULT 7000
)
RETURNS TABLE(
  location_id BIGINT,
  name TEXT,
  vicinity TEXT,
  lat NUMERIC,
  lng NUMERIC,
  created_at TIMESTAMP,
  cuisine TEXT,
  rating REAL,
  user_ratings_total NUMERIC,
  price_level NUMERIC,
  photo_reference TEXT,
  saved_count SMALLINT,
  google_place_id TEXT,
  business_status TEXT,
  editorial_summary TEXT,
  website TEXT,
  international_phone_number TEXT,
  types TEXT,
  opening_hours_text TEXT[],
  opening_hours_periods JSONB,
  open_now BOOLEAN,
  cuisine_detected TEXT,
  cuisine_source TEXT,
  cuisine_primary TEXT,
  is_open_late BOOLEAN,
  is_open_early BOOLEAN,
  is_sunday_open BOOLEAN,
  price_bucket TEXT,
  derived_attributes JSONB,
  data_version TEXT,
  ingested_at TIMESTAMPTZ,
  emoji TEXT,
  vibe_vector REAL[],
  dietary_requirement_vector INT[],
  distance_km FLOAT,
  quality_score FLOAT
) AS $$
BEGIN
  RETURN QUERY
  SELECT
    l.location_id,
    l.name,
    l.vicinity,
    l.lat,
    l.lng,
    l.created_at,
    l.cuisine,
    l.rating,
    l.user_ratings_total,
    l.price_level,
    l.photo_reference,
    l.saved_count,
    l.google_place_id,
    l.business_status,
    l.editorial_summary,
    l.website,
    l.international_phone_number,
    l.types,
    l.opening_hours_text,
    l.opening_hours_periods,
    l.open_now,
    l.cuisine_detected,
    l.cuisine_source,
    l.cuisine_primary,
    l.is_open_late,
    l.is_open_early,
    l.is_sunday_open,
    l.price_bucket,
    l.derived_attributes,
    l.data_version,
    l.ingested_at,
    l.emoji,
    l.vibe_vector,
    l.dietary_requirement_vector,
    (ST_Distance(
      l.geog,
      ST_SetSRID(ST_MakePoint(center_lng, center_lat), 4326)::geography
    ) / 1000.0)::FLOAT AS distance_km,
    COALESCE(lpa.quality_score, 0.0)::FLOAT AS quality_score
  FROM locations l
  LEFT JOIN location_popularity_app lpa ON lpa.location_id = l.location_id
  WHERE ST_DWithin(
    l.geog,
    ST_SetSRID(ST_MakePoint(center_lng, center_lat), 4326)::geography,
    radius_meters
  )
  ORDER BY distance_km ASC
  LIMIT result_limit;
END;
$$ LANGUAGE plpgsql STABLE;

GRANT EXECUTE ON FUNCTION get_locations_with_quality(
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, INT
) TO authenticated, anon, service_role;
