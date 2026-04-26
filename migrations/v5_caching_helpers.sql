-- ============================================================================
-- V5: CACHING HELPERS
-- ============================================================================
-- Sibling RPC for the caching layer. Returns ONLY the fill_set
-- (locations within radius that are NOT in location_popularity_app).
-- The Python caching layer sources the engaged primary_set from a Redis
-- snapshot of LPA, then calls this RPC for the fill.
--
-- Non-destructive: additive. `get_locations_with_pillars` keeps working
-- for callers that haven't migrated to the snapshot path.

CREATE OR REPLACE FUNCTION get_fill_locations(
    center_lat DOUBLE PRECISION,
    center_lng DOUBLE PRECISION,
    radius_meters DOUBLE PRECISION,
    result_limit INT DEFAULT 1000
)
RETURNS TABLE(
    location_id BIGINT,
    name TEXT,
    vicinity TEXT,
    lat NUMERIC,
    lng NUMERIC,
    cuisine TEXT,
    cuisine_primary TEXT,
    rating REAL,
    user_ratings_total NUMERIC,
    price_level NUMERIC,
    google_place_id TEXT,
    types TEXT,
    emoji TEXT,
    vibe_vector INT[],
    dietary_requirement_vector INT[],
    distance_km FLOAT,
    -- Fill-set rows: pillar scores zero/null, has_app_signal always FALSE.
    app_engagement_score NUMERIC,
    google_baseline_score NUMERIC,
    video_insight_score NUMERIC,
    share_count INTEGER,
    has_app_signal BOOLEAN,
    quality_score NUMERIC
) AS $$
BEGIN
    RETURN QUERY
    WITH point AS (
        SELECT ST_SetSRID(ST_MakePoint(center_lng, center_lat), 4326)::geography AS g
    )
    SELECT
        l.location_id,
        l.name,
        l.vicinity,
        l.lat,
        l.lng,
        l.cuisine,
        l.cuisine_primary,
        l.rating,
        l.user_ratings_total,
        l.price_level,
        l.google_place_id,
        l.types,
        l.emoji,
        l.vibe_vector,
        l.dietary_requirement_vector,
        ST_Distance(l.geog, (SELECT g FROM point)) / 1000.0 AS distance_km,
        0.0::NUMERIC AS app_engagement_score,
        compute_google_baseline_score(l.rating, l.user_ratings_total) AS google_baseline_score,
        0.0::NUMERIC AS video_insight_score,
        0::INTEGER AS share_count,
        FALSE AS has_app_signal,
        -- Legacy quality_score = google_baseline only (no app signal yet)
        compute_google_baseline_score(l.rating, l.user_ratings_total) * 0.30 AS quality_score
    FROM public.locations l
    WHERE l.geog IS NOT NULL
      AND ST_DWithin(l.geog, (SELECT g FROM point), radius_meters)
      AND NOT EXISTS (
          SELECT 1 FROM public.location_popularity_app lp
          WHERE lp.location_id = l.location_id
      )
    ORDER BY ST_Distance(l.geog, (SELECT g FROM point)) ASC
    LIMIT result_limit;
END;
$$ LANGUAGE plpgsql STABLE;


GRANT EXECUTE ON FUNCTION get_fill_locations(DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, INT)
    TO authenticated, anon;
