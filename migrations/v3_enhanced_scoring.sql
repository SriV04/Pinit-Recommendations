-- ============================================================================
-- V3: ENHANCED SCORING - Social, Collaborative, Improved Quality
-- ============================================================================

-- ============================================================================
-- 1. ENHANCED QUALITY SCORE
-- ============================================================================
-- Incorporates: save/dislike ratio, review velocity, cold-start bonus,
-- anti-mispricing cap

CREATE OR REPLACE FUNCTION compute_quality_score(
  p_rating FLOAT,
  p_user_ratings_total NUMERIC,
  p_saved_count SMALLINT DEFAULT 0,
  p_saves_count_app INTEGER DEFAULT 0,
  p_dislikes_count_app INTEGER DEFAULT 0,
  p_recent_reviews_90d INTEGER DEFAULT 0,
  p_total_app_reviews INTEGER DEFAULT 0,
  p_location_age_days INTEGER DEFAULT 365
) RETURNS FLOAT AS $$
DECLARE
  bayesian_rating FLOAT;
  review_trust FLOAT;
  velocity_boost FLOAT;
  rating_score FLOAT;
  social_score FLOAT;
  saves_total INTEGER;
  cold_start_bonus FLOAT;
BEGIN
  -- Bayesian rating: shrink towards 3.8 prior with 50 pseudo-reviews
  bayesian_rating := (COALESCE(p_rating, 3.8) * COALESCE(p_user_ratings_total, 0) + 3.8 * 50)
                     / (COALESCE(p_user_ratings_total, 0) + 50);

  -- Review trust: log-power curve
  review_trust := LEAST(
    0.95 * POWER(LN(1 + COALESCE(p_user_ratings_total, 0)) / LN(501), 0.6),
    1.0
  );

  -- Review velocity boost: recent reviews indicate active/trending location
  IF COALESCE(p_total_app_reviews, 0) > 0 THEN
    velocity_boost := 1.0 + 0.3 * (COALESCE(p_recent_reviews_90d, 0)::FLOAT / (p_total_app_reviews + 1));
  ELSE
    velocity_boost := 1.0;
  END IF;
  review_trust := LEAST(review_trust * velocity_boost, 1.0);

  -- Anti-mispricing: cap trust so volume can't overwhelm quality
  -- A 3.5-star place with 5000 reviews can't outscore a 4.8-star with 100
  rating_score := (bayesian_rating / 5.0) * LEAST(review_trust, 0.85 + 0.15 * (bayesian_rating / 5.0));

  -- Social proof from in-app engagement (prefer app data over Google saved_count)
  saves_total := GREATEST(COALESCE(p_saves_count_app, 0), COALESCE(p_saved_count, 0)::INTEGER);
  social_score := LEAST(
    LN(1 + saves_total) / LN(100),
    1.0
  );
  -- Penalize locations with high dislike ratio
  IF (saves_total + COALESCE(p_dislikes_count_app, 0)) > 0 THEN
    social_score := social_score * (1.0 - COALESCE(p_dislikes_count_app, 0)::FLOAT / (saves_total + COALESCE(p_dislikes_count_app, 0) + 1));
  END IF;

  -- Cold-start discovery bonus: new locations get temporary visibility boost
  cold_start_bonus := 0.0;
  IF COALESCE(p_location_age_days, 365) < 30 AND COALESCE(p_total_app_reviews, 0) < 5 THEN
    cold_start_bonus := 0.1 * (1.0 - COALESCE(p_location_age_days, 30)::FLOAT / 30.0);
  END IF;

  RETURN LEAST(
    (0.6 * rating_score) + (0.4 * social_score) + cold_start_bonus,
    1.0
  );
END;
$$ LANGUAGE plpgsql IMMUTABLE;


-- ============================================================================
-- 2. UPDATED get_locations_with_quality
-- ============================================================================
-- Now joins location_popularity_app and aggregates recent reviews

DROP FUNCTION IF EXISTS get_locations_with_quality(DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, INT);

CREATE OR REPLACE FUNCTION get_locations_with_quality(
  center_lat DOUBLE PRECISION,
  center_lng DOUBLE PRECISION,
  radius_meters DOUBLE PRECISION,
  result_limit INT DEFAULT 100000
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
  review_language_counts_json JSONB,
  is_open_late BOOLEAN,
  is_open_early BOOLEAN,
  is_sunday_open BOOLEAN,
  price_bucket TEXT,
  derived_attributes JSONB,
  data_version TEXT,
  ingested_at TIMESTAMP,
  emoji TEXT,
  vibe_vector INT[],
  dietary_requirement_vector INT[],
  distance_km FLOAT,
  quality_score FLOAT
) AS $$
BEGIN
  RETURN QUERY
  WITH nearby AS (
    SELECT
      l.*,
      ST_Distance(
        l.geog,
        ST_SetSRID(ST_MakePoint(center_lng, center_lat), 4326)::geography
      ) / 1000.0 AS dist_km
    FROM locations l
    WHERE ST_DWithin(
      l.geog,
      ST_SetSRID(ST_MakePoint(center_lng, center_lat), 4326)::geography,
      radius_meters
    )
  ),
  popularity AS (
    SELECT
      lp.location_id,
      COALESCE(lp.saves_count, 0) AS saves_count_app,
      COALESCE(lp.dislikes_count, 0) AS dislikes_count_app
    FROM location_popularity_app lp
  ),
  review_stats AS (
    SELECT
      lr.location_id,
      COUNT(*) AS total_app_reviews,
      COUNT(*) FILTER (WHERE lr.created_at >= NOW() - INTERVAL '90 days') AS recent_reviews_90d
    FROM location_reviews lr
    GROUP BY lr.location_id
  )
  SELECT
    n.location_id,
    n.name,
    n.vicinity,
    n.lat,
    n.lng,
    n.created_at,
    n.cuisine,
    n.rating,
    n.user_ratings_total,
    n.price_level,
    n.photo_reference,
    n.saved_count,
    n.google_place_id,
    n.business_status,
    n.editorial_summary,
    n.website,
    n.international_phone_number,
    n.types,
    n.opening_hours_text,
    n.opening_hours_periods,
    n.open_now,
    n.cuisine_detected,
    n.cuisine_source,
    n.cuisine_primary,
    n.review_language_counts_json,
    n.is_open_late,
    n.is_open_early,
    n.is_sunday_open,
    n.price_bucket,
    n.derived_attributes,
    n.data_version,
    n.ingested_at,
    n.emoji,
    n.vibe_vector,
    n.dietary_requirement_vector,
    n.dist_km AS distance_km,
    compute_quality_score(
      n.rating,
      n.user_ratings_total,
      n.saved_count,
      COALESCE(p.saves_count_app, 0),
      COALESCE(p.dislikes_count_app, 0),
      COALESCE(rs.recent_reviews_90d, 0)::INTEGER,
      COALESCE(rs.total_app_reviews, 0)::INTEGER,
      EXTRACT(DAY FROM NOW() - COALESCE(n.created_at, NOW() - INTERVAL '365 days'))::INTEGER
    ) AS quality_score
  FROM nearby n
  LEFT JOIN popularity p ON p.location_id = n.location_id
  LEFT JOIN review_stats rs ON rs.location_id = n.location_id
  ORDER BY n.dist_km ASC
  LIMIT result_limit;
END;
$$ LANGUAGE plpgsql STABLE;


-- ============================================================================
-- 3. LOCATION SIMILARITIES TABLE (for collaborative filtering)
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.location_similarities (
    location_id_a BIGINT NOT NULL REFERENCES locations(location_id),
    location_id_b BIGINT NOT NULL REFERENCES locations(location_id),
    similarity_score FLOAT NOT NULL,
    co_save_count INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT location_similarities_pkey PRIMARY KEY (location_id_a, location_id_b)
);

CREATE INDEX IF NOT EXISTS idx_loc_sim_a ON location_similarities(location_id_a);
CREATE INDEX IF NOT EXISTS idx_loc_sim_b ON location_similarities(location_id_b);

-- ============================================================================
-- 4. GRANT PERMISSIONS
-- ============================================================================

GRANT EXECUTE ON FUNCTION compute_quality_score(FLOAT, NUMERIC, SMALLINT, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER) TO authenticated;
GRANT EXECUTE ON FUNCTION compute_quality_score(FLOAT, NUMERIC, SMALLINT, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER) TO anon;
GRANT EXECUTE ON FUNCTION get_locations_with_quality TO authenticated;
GRANT EXECUTE ON FUNCTION get_locations_with_quality TO anon;

GRANT SELECT, INSERT, UPDATE, DELETE ON location_similarities TO authenticated;
GRANT SELECT ON location_similarities TO anon;
