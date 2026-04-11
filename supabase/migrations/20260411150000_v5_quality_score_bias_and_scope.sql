-- ============================================================================
-- V5: QUALITY SCORE — 0.5 BIAS + SCOPED REFRESH
-- ============================================================================
-- Iterates on v4:
--   1. Every scored location now starts at a 0.5 bias and is pushed up or
--      down from there based on engagement.
--   2. Dislikes become a SUBTRACTIVE signal (they previously multiplied with
--      positives, meaning a place with 0 positives and many dislikes stayed
--      at 0 instead of dropping below 0.5).
--   3. Google multiplier applies ASYMMETRICALLY: sub-4-star Google amplifies
--      a negative engagement instead of softening it (otherwise bad Google
--      would rescue disliked places).
--   4. refresh_location_quality_scores() only scores locations that appear
--      in user_location_actions. Locations with zero engagement are skipped.
-- ============================================================================


-- ============================================================================
-- 1. REPLACE compute_app_quality_score
-- ============================================================================
-- Signature unchanged — safe to CREATE OR REPLACE.
--
-- Tuning handles:
--   * Bias:                  0.5
--   * Saturation thresholds: 50 / 100 / 200 / 50 (shares / been_to / saves / dislikes)
--   * Component weights:     +0.30 / +0.30 / +0.20 / +0.20 / -0.30
--   * Google penalty levels: 0.75 (no data), 0.5 (sub-4-star)

CREATE OR REPLACE FUNCTION compute_app_quality_score(
  p_shares_count   INTEGER,
  p_been_to_count  INTEGER,
  p_saves_count    INTEGER,
  p_review_signal  NUMERIC,   -- pre-aggregated sentiment in [-1, +1]
  p_dislikes_count INTEGER,
  p_google_rating  REAL,
  p_google_total   NUMERIC
) RETURNS NUMERIC AS $$
DECLARE
  s_shares     NUMERIC;
  s_been_to    NUMERIC;
  s_saves      NUMERIC;
  s_dislikes   NUMERIC;
  engagement   NUMERIC;
  google_mult  NUMERIC;
  contribution NUMERIC;
BEGIN
  -- 1. Log-normalize each count signal to [0, 1]
  s_shares   := LEAST(LN(1 + COALESCE(p_shares_count,   0)) / LN(1 + 50),  1.0);
  s_been_to  := LEAST(LN(1 + COALESCE(p_been_to_count,  0)) / LN(1 + 100), 1.0);
  s_saves    := LEAST(LN(1 + COALESCE(p_saves_count,    0)) / LN(1 + 200), 1.0);
  s_dislikes := LEAST(LN(1 + COALESCE(p_dislikes_count, 0)) / LN(1 + 50),  1.0);

  -- 2. Signed engagement in roughly [-0.50, +1.00]:
  --    positives lift the score, dislikes and bad reviews drop it.
  engagement := (0.30 * s_shares)
              + (0.30 * s_been_to)
              + (0.20 * s_saves)
              + (0.20 * COALESCE(p_review_signal, 0))
              - (0.30 * s_dislikes);

  -- 3. Google multiplier — three states
  IF p_google_total IS NULL OR p_google_total < 5 THEN
    google_mult := 0.75;        -- no Google data: mild penalty
  ELSIF p_google_rating IS NULL OR p_google_rating < 4.0 THEN
    google_mult := 0.5;         -- confirmed sub-4-star: bigger penalty
  ELSE
    google_mult := 1.0;         -- 4★ or better with real data: no penalty
  END IF;

  -- 4. Apply Google asymmetrically:
  --    * positive engagement → multiplied DOWN by google_mult
  --    * negative engagement → multiplied UP by (2 - google_mult)
  --      so sub-4-star Google AMPLIFIES dislike-driven penalties rather than
  --      softening them.
  IF engagement >= 0 THEN
    contribution := engagement * google_mult;
  ELSE
    contribution := engagement * (2.0 - google_mult);
  END IF;

  -- 5. Add the 0.5 bias and clamp to [0, 1]
  RETURN GREATEST(0.0, LEAST(1.0, 0.5 + contribution));
END;
$$ LANGUAGE plpgsql IMMUTABLE;


-- ============================================================================
-- 2. REPLACE refresh_location_quality_scores
-- ============================================================================
-- Now driven by action_stats instead of the full locations table — only
-- locations that appear in user_location_actions get a fresh score on each
-- run. Locations without any engagement are left untouched.

CREATE OR REPLACE FUNCTION refresh_location_quality_scores()
RETURNS INTEGER AS $$
DECLARE
  affected INTEGER;
BEGIN
  WITH action_stats AS (
    -- Drives the whole refresh.
    SELECT
      location_id,
      COUNT(*) FILTER (WHERE action = 'save')              AS saves_count,
      COUNT(*) FILTER (WHERE action = 'been_to')           AS been_to_count,
      COUNT(*) FILTER (WHERE action = 'dislike')           AS dislikes_count,
      COUNT(*) FILTER (WHERE source_video_url IS NOT NULL) AS shares_count
    FROM user_location_actions
    GROUP BY location_id
  ),
  review_stats AS (
    SELECT
      location_id,
      SUM(
        CASE
          WHEN rating IS NULL THEN 0
          WHEN rating < 5   THEN -((5 - rating) / 5.0)
          WHEN rating > 6.5 THEN  ((rating - 6.5) / 3.5)
          ELSE 0
        END
      )::NUMERIC / 10.0 AS review_signal_raw
    FROM location_reviews
    GROUP BY location_id
  )
  INSERT INTO location_popularity_app (
    location_id, saves_count, dislikes_count, been_to_count, quality_score, updated_at
  )
  SELECT
    a.location_id,
    a.saves_count::INTEGER,
    a.dislikes_count::INTEGER,
    a.been_to_count::INTEGER,
    compute_app_quality_score(
      a.shares_count::INTEGER,
      a.been_to_count::INTEGER,
      a.saves_count::INTEGER,
      GREATEST(-1.0, LEAST(1.0, COALESCE(r.review_signal_raw, 0))),
      a.dislikes_count::INTEGER,
      l.rating,
      l.user_ratings_total
    ),
    NOW()
  FROM action_stats a
  INNER JOIN locations l    ON l.location_id = a.location_id
  LEFT JOIN  review_stats r ON r.location_id = a.location_id
  ON CONFLICT (location_id) DO UPDATE SET
    saves_count    = EXCLUDED.saves_count,
    dislikes_count = EXCLUDED.dislikes_count,
    been_to_count  = EXCLUDED.been_to_count,
    quality_score  = EXCLUDED.quality_score,
    updated_at     = NOW();

  GET DIAGNOSTICS affected = ROW_COUNT;
  RETURN affected;
END;
$$ LANGUAGE plpgsql;


-- ============================================================================
-- 3. RE-SEED IMMEDIATELY
-- ============================================================================
-- Re-run with the new formula so the table reflects the bias without waiting
-- for the nightly cron.

SELECT refresh_location_quality_scores();
