-- ============================================================================
-- DATABASE-SIDE RANKING FOR PROXIMAL RECOMMENDATIONS (V2)
-- ============================================================================
-- Updated to return complete location records
-- Fixed compute_quality_score to match actual table schema

-- ============================================================================
-- 1. VECTOR SIMILARITY FUNCTIONS
-- ============================================================================

-- Centered Cosine Similarity (Pearson Correlation)
-- Used for vibe matching - handles cold start gracefully
CREATE OR REPLACE FUNCTION centered_cosine_similarity(
  vec1 INT[],
  vec2 INT[]
) RETURNS FLOAT AS $$
DECLARE
  mean1 FLOAT;
  mean2 FLOAT;
  centered1 FLOAT[];
  centered2 FLOAT[];
  dot_product FLOAT := 0.0;
  norm1 FLOAT := 0.0;
  norm2 FLOAT := 0.0;
  i INT;
  max_len INT;
BEGIN
  -- Handle null/empty vectors
  IF vec1 IS NULL OR vec2 IS NULL OR array_length(vec1, 1) IS NULL OR array_length(vec2, 1) IS NULL THEN
    RETURN 0.0;
  END IF;

  -- Get max length (handle dimension mismatch by padding with zeros conceptually)
  max_len := GREATEST(array_length(vec1, 1), array_length(vec2, 1));

  -- Calculate means
  mean1 := (SELECT AVG(v) FROM unnest(vec1) v);
  mean2 := (SELECT AVG(v) FROM unnest(vec2) v);

  -- Center vectors and compute dot product and norms
  FOR i IN 1..max_len LOOP
    DECLARE
      v1 FLOAT := COALESCE(vec1[i], 0)::FLOAT - mean1;
      v2 FLOAT := COALESCE(vec2[i], 0)::FLOAT - mean2;
    BEGIN
      dot_product := dot_product + (v1 * v2);
      norm1 := norm1 + (v1 * v1);
      norm2 := norm2 + (v2 * v2);
    END;
  END LOOP;

  -- Handle zero norm (uniform vectors) - return 0.0
  IF norm1 = 0 OR norm2 = 0 THEN
    RETURN 0.0;
  END IF;

  -- Compute centered cosine similarity
  DECLARE
    similarity FLOAT := dot_product / (SQRT(norm1) * SQRT(norm2));
  BEGIN
    -- Normalize from [-1, 1] to [0, 1]
    RETURN GREATEST(0.0, LEAST(1.0, (similarity + 1.0) / 2.0));
  END;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- Normalized Dot Product
-- Used for dietary requirement matching
CREATE OR REPLACE FUNCTION normalized_dot_product(
  vec1 INT[],
  vec2 INT[]
) RETURNS FLOAT AS $$
DECLARE
  dot_prod FLOAT := 0.0;
  max_len INT;
  max_possible FLOAT;
  i INT;
BEGIN
  -- Handle null/empty vectors
  IF vec1 IS NULL OR vec2 IS NULL OR array_length(vec1, 1) IS NULL OR array_length(vec2, 1) IS NULL THEN
    RETURN 0.0;
  END IF;

  -- Get max length
  max_len := GREATEST(array_length(vec1, 1), array_length(vec2, 1));

  -- Compute dot product
  FOR i IN 1..max_len LOOP
    dot_prod := dot_prod + (COALESCE(vec1[i], 0)::FLOAT * COALESCE(vec2[i], 0)::FLOAT);
  END LOOP;

  -- Normalize to [0, 1] assuming values are in 0-100 range
  max_possible := max_len * 100.0 * 100.0;

  IF max_possible = 0 THEN
    RETURN 0.0;
  END IF;

  RETURN GREATEST(0.0, LEAST(1.0, dot_prod / max_possible));
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- ============================================================================
-- 2. QUALITY SCORE FUNCTION (SIMPLIFIED)
-- ============================================================================
-- Uses only columns that actually exist in the locations table

CREATE OR REPLACE FUNCTION compute_quality_score(
  rating FLOAT,
  user_ratings_total NUMERIC,
  saved_count SMALLINT DEFAULT 0
) RETURNS FLOAT AS $$
DECLARE
  bayesian_rating FLOAT;
  review_trust FLOAT;
  rating_score FLOAT;
  social_score FLOAT;
BEGIN
  -- Bayesian rating: shrink towards 3.8 prior with 50 pseudo-reviews
  bayesian_rating := (COALESCE(rating, 3.8) * COALESCE(user_ratings_total, 0) + 3.8 * 50)
                     / (COALESCE(user_ratings_total, 0) + 50);

  -- Review trust: log-power curve, punishes low reviews hard
  -- 0→0.0, 3→0.34, 10→0.52, 30→0.70, 100→0.83, 500→0.95
  review_trust := LEAST(
    0.95 * POWER(LN(1 + COALESCE(user_ratings_total, 0)) / LN(501), 0.6),
    1.0
  );

  -- Rating score: bayesian rating normalised to 0-1, scaled by review trust
  rating_score := (bayesian_rating / 5.0) * review_trust;

  -- Pinit social proof: log scale, ~100 saves maxes out
  social_score := LEAST(
    LN(1 + COALESCE(saved_count, 0)) / LN(100),
    1.0
  );

  -- Final score: 60% rating (includes trust), 40% social proof
  RETURN LEAST(
    (0.6 * rating_score) + (0.4 * social_score),
    1.0
  );
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- ============================================================================
-- 3. MAIN RANKING FUNCTION (RETURNS COMPLETE LOCATION RECORDS)
-- ============================================================================

CREATE OR REPLACE FUNCTION get_ranked_proximal_recommendations(
  p_supabase_id UUID,
  center_lat DOUBLE PRECISION,
  center_lng DOUBLE PRECISION,
  radius_meters DOUBLE PRECISION,
  quality_weight FLOAT DEFAULT 0.33,
  vibe_weight FLOAT DEFAULT 0.34,
  dietary_weight FLOAT DEFAULT 0.33,
  result_limit INT DEFAULT 100000
)
RETURNS TABLE(
  -- All location columns
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

  -- Scoring columns
  distance_km FLOAT,
  vibe_score FLOAT,
  dietary_score FLOAT,
  quality_score FLOAT,
  final_score FLOAT,
  rank INT
) AS $$
BEGIN
  RETURN QUERY
  WITH user_data AS (
    -- Get user's vector affinities
    SELECT
      vibe_tag_affinity AS vibe_affinity_vector,
      dietary_requirement_tag_affinity AS dietary_requirement_affinity_vector
    FROM users
    WHERE supabase_id = p_supabase_id
  ),
  nearby_locations AS (
    -- Get ALL location columns within radius with distance
    SELECT
      l.*,  -- Select all location columns
      -- Calculate distance in kilometers
      ST_Distance(
        l.geog,
        ST_SetSRID(ST_MakePoint(center_lng, center_lat), 4326)::geography
      ) / 1000.0 AS distance_km
    FROM locations l
    WHERE ST_DWithin(
      l.geog,
      ST_SetSRID(ST_MakePoint(center_lng, center_lat), 4326)::geography,
      radius_meters
    )
  ),
  scored_locations AS (
    -- Compute all component scores
    SELECT
      nl.*,
      -- Vibe score using centered cosine similarity
      COALESCE(
        centered_cosine_similarity(
          u.vibe_affinity_vector,
          nl.vibe_vector
        ),
        0.0
      ) AS vibe_score,
      -- Dietary score using normalized dot product
      COALESCE(
        normalized_dot_product(
          u.dietary_requirement_affinity_vector,
          nl.dietary_requirement_vector
        ),
        0.0
      ) AS dietary_score,
      -- Quality score based on ratings and social proof
      compute_quality_score(
        nl.rating,
        nl.user_ratings_total,
        nl.saved_count
      ) AS quality_score
    FROM nearby_locations nl
    CROSS JOIN user_data u
  ),
  final_ranked AS (
    -- Calculate final weighted score and rank
    SELECT
      sl.*,
      (
        quality_weight * sl.quality_score +
        vibe_weight * sl.vibe_score +
        dietary_weight * sl.dietary_score
      ) AS final_score
    FROM scored_locations sl
    ORDER BY
      (
        quality_weight * sl.quality_score +
        vibe_weight * sl.vibe_score +
        dietary_weight * sl.dietary_score
      ) DESC,
      sl.distance_km ASC
    LIMIT result_limit
  )
  SELECT
    fr.location_id,
    fr.name,
    fr.vicinity,
    fr.lat,
    fr.lng,
    fr.created_at,
    fr.cuisine,
    fr.rating,
    fr.user_ratings_total,
    fr.price_level,
    fr.photo_reference,
    fr.saved_count,
    fr.google_place_id,
    fr.business_status,
    fr.editorial_summary,
    fr.website,
    fr.international_phone_number,
    fr.types,
    fr.opening_hours_text,
    fr.opening_hours_periods,
    fr.open_now,
    fr.cuisine_detected,
    fr.cuisine_source,
    fr.cuisine_primary,
    fr.review_language_counts_json,
    fr.is_open_late,
    fr.is_open_early,
    fr.is_sunday_open,
    fr.price_bucket,
    fr.derived_attributes,
    fr.data_version,
    fr.ingested_at,
    fr.emoji,
    fr.vibe_vector,
    fr.dietary_requirement_vector,
    fr.distance_km,
    fr.vibe_score,
    fr.dietary_score,
    fr.quality_score,
    fr.final_score,
    ROW_NUMBER() OVER (ORDER BY fr.final_score DESC, fr.distance_km ASC)::INT AS rank
  FROM final_ranked fr;
END;
$$ LANGUAGE plpgsql STABLE;

-- ============================================================================
-- 4. INDEXES FOR PERFORMANCE
-- ============================================================================

-- Spatial index (should already exist, but ensure it's there)
CREATE INDEX IF NOT EXISTS idx_locations_geog ON locations USING GIST (geog);

-- Index on supabase_id for fast user lookups (primary key already indexed)
-- CREATE INDEX IF NOT EXISTS idx_users_supabase_id ON users (supabase_id); -- Not needed, already PK

-- Index on google_place_id for lookups
CREATE INDEX IF NOT EXISTS idx_locations_google_place_id ON locations (google_place_id);

-- ============================================================================
-- 5. GRANT PERMISSIONS
-- ============================================================================

GRANT EXECUTE ON FUNCTION centered_cosine_similarity TO authenticated;
GRANT EXECUTE ON FUNCTION normalized_dot_product TO authenticated;
GRANT EXECUTE ON FUNCTION compute_quality_score TO authenticated;
GRANT EXECUTE ON FUNCTION get_ranked_proximal_recommendations TO authenticated;

GRANT EXECUTE ON FUNCTION centered_cosine_similarity TO anon;
GRANT EXECUTE ON FUNCTION normalized_dot_product TO anon;
GRANT EXECUTE ON FUNCTION compute_quality_score TO anon;
GRANT EXECUTE ON FUNCTION get_ranked_proximal_recommendations TO anon;

-- ============================================================================
-- 6. TESTING
-- ============================================================================

-- Test vector similarity functions
SELECT centered_cosine_similarity(ARRAY[80, 90, 70], ARRAY[80, 90, 70]); -- Should be ~1.0
SELECT centered_cosine_similarity(ARRAY[50, 50, 50], ARRAY[80, 20, 90]); -- Should be ~0.0 (cold start)
SELECT normalized_dot_product(ARRAY[100, 100, 100], ARRAY[100, 100, 100]); -- Should be ~1.0

-- Test quality score
SELECT compute_quality_score(4.5, 1000, 50); -- Should be high score

-- Test full ranking function
-- Replace 'your-uuid-here' with an actual user UUID from your database
SELECT * FROM get_ranked_proximal_recommendations(
  'your-uuid-here'::UUID,  -- supabase_id (UUID)
  51.5074,                  -- London latitude
  -0.1278,                  -- London longitude
  15000,                    -- 15km in meters
  0.33,                     -- quality_weight
  0.34,                     -- vibe_weight
  0.33,                     -- dietary_weight
  20                        -- return top 20
);
