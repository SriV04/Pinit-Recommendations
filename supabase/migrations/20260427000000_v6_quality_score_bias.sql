-- ============================================================================
-- V6: QUALITY SCORE → MANUAL ADDITIVE BIAS, CRON UPDATES PILLARS ONLY
-- ============================================================================
-- The `quality_score` column on location_popularity_app changes role:
--
--   BEFORE (v4/v5)  : a [0, 1] composite quality estimate, recomputed on
--                     every cron run. Used as a weighted ranking component.
--   AFTER  (v6)     : a small manual bias in [-0.15, +0.15] used purely as
--                     an ADDITIVE adjustment to the final recommendation
--                     score. Set manually to push specific places up/down.
--                     The cron NEVER overwrites it.
--
-- The cron's actual job is now to keep the engagement counts and pillar
-- columns fresh (`saves_count`, `dislikes_count`, `been_to_count`,
-- `share_count`, `app_engagement_score`, `google_baseline_score`,
-- `video_insight_score`). All five components of the new ranker live in
-- those columns; quality_score is the manual override on top.
--
-- The cron now refreshes locations that EITHER have engagement OR already
-- exist in location_popularity_app (so manually-biased rows with no
-- actions still keep their counts/pillars in sync).
-- ============================================================================


-- ============================================================================
-- 1. ONE-TIME DATA MIGRATION on existing quality_score values
-- ============================================================================
-- Existing values are in [0, 1] and were used as a weighted component.
-- Re-bucket them into the new bias range so manual review is rarely needed.

UPDATE public.location_popularity_app
SET quality_score = CASE
        WHEN quality_score > 0.58 THEN  0.10
        WHEN quality_score < 0.49 THEN -0.05
        ELSE                             0.00
    END,
    updated_at = NOW();


-- ============================================================================
-- 2. REPLACE refresh_location_quality_scores
-- ============================================================================
-- New behaviour:
--   * Targets = (locations with any engagement) ∪ (locations already in LPA)
--   * Updates: saves_count, dislikes_count, been_to_count, share_count,
--              app_engagement_score, google_baseline_score, video_insight_score
--   * NEVER touches quality_score (manual bias is preserved)

CREATE OR REPLACE FUNCTION refresh_location_quality_scores()
RETURNS INTEGER AS $$
DECLARE
    affected INTEGER;
BEGIN
    WITH action_stats AS (
        SELECT
            location_id,
            COUNT(*) FILTER (WHERE action = 'save')              AS saves_count,
            COUNT(*) FILTER (WHERE action = 'been_to')           AS been_to_count,
            COUNT(*) FILTER (WHERE action = 'dislike')           AS dislikes_count,
            -- share_count = social-media saves only (matches v4 trigger logic)
            COUNT(*) FILTER (
                WHERE action = 'save'
                  AND saved_method IN ('tiktok', 'instagram')
            )                                                     AS share_count
        FROM public.user_location_actions
        GROUP BY location_id
    ),
    targets AS (
        -- Anything with engagement, OR already in LPA (manual biases etc.)
        SELECT location_id FROM action_stats
        UNION
        SELECT location_id FROM public.location_popularity_app
    )
    INSERT INTO public.location_popularity_app AS lp (
        location_id,
        saves_count,
        dislikes_count,
        been_to_count,
        share_count,
        app_engagement_score,
        google_baseline_score,
        video_insight_score,
        updated_at
    )
    SELECT
        t.location_id,
        COALESCE(a.saves_count,    0)::INTEGER,
        COALESCE(a.dislikes_count, 0)::INTEGER,
        COALESCE(a.been_to_count,  0)::INTEGER,
        COALESCE(a.share_count,    0)::INTEGER,
        compute_app_engagement_score(
            COALESCE(a.saves_count,    0)::INTEGER,
            COALESCE(a.dislikes_count, 0)::INTEGER,
            COALESCE(a.been_to_count,  0)::INTEGER
        ),
        compute_google_baseline_score(l.rating, l.user_ratings_total),
        compute_video_insight_score(t.location_id),
        NOW()
    FROM targets t
    INNER JOIN public.locations l       ON l.location_id = t.location_id
    LEFT JOIN  action_stats     a       ON a.location_id = t.location_id
    ON CONFLICT (location_id) DO UPDATE SET
        saves_count            = EXCLUDED.saves_count,
        dislikes_count         = EXCLUDED.dislikes_count,
        been_to_count          = EXCLUDED.been_to_count,
        share_count            = EXCLUDED.share_count,
        app_engagement_score   = EXCLUDED.app_engagement_score,
        google_baseline_score  = EXCLUDED.google_baseline_score,
        video_insight_score    = EXCLUDED.video_insight_score,
        updated_at             = NOW()
        -- NOTE: quality_score is intentionally NOT in the SET list. The
        -- manual bias is preserved across cron runs.
    ;

    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected;
END;
$$ LANGUAGE plpgsql;


-- ============================================================================
-- 3. REPLACE get_locations_with_pillars
-- ============================================================================
-- Surface `lp.quality_score` directly as the manual bias (no longer the
-- v4 blended formula). Two-pass inventory unchanged.
--
-- Performance note: this keeps the two-pass inventory policy but uses the
-- PostGIS `<->` KNN operator and a slim projection. Ordering by
-- `ST_Distance(...)` forced a full in-radius sort and was timing out under
-- Supabase's statement limit on cold reads.
--
-- The RETURNS TABLE shape changes (replaces `quality_score` with
-- `quality_bias`), so a plain CREATE OR REPLACE fails with
-- "cannot change return type of existing function". Drop first.

DROP FUNCTION IF EXISTS get_locations_with_pillars(
    DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, INT
);

CREATE OR REPLACE FUNCTION get_locations_with_pillars(
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
    vibe_vector REAL[],
    dietary_requirement_vector INT[],
    distance_km FLOAT,
    -- Pillar scores (precomputed, 0..1)
    app_engagement_score NUMERIC,
    google_baseline_score NUMERIC,
    video_insight_score NUMERIC,
    share_count INTEGER,
    has_app_signal BOOLEAN,
    -- Manual additive bias in [-0.15, +0.15]; 0 by default.
    quality_bias NUMERIC
) LANGUAGE sql STABLE
AS $$
    WITH center AS (
        SELECT ST_SetSRID(ST_MakePoint(center_lng, center_lat), 4326)::geography AS g
    ),
    primary_set AS (
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
            (ST_Distance(lp.geog, center.g) / 1000.0)::FLOAT AS distance_km,
            COALESCE(lp.app_engagement_score, 0.0) AS app_engagement_score,
            COALESCE(
                lp.google_baseline_score,
                compute_google_baseline_score(l.rating, l.user_ratings_total)
            ) AS google_baseline_score,
            COALESCE(lp.video_insight_score, 0.0) AS video_insight_score,
            COALESCE(lp.share_count, 0) AS share_count,
            TRUE AS has_app_signal,
            COALESCE(lp.quality_score, 0.0) AS quality_bias
        FROM public.location_popularity_app lp
        INNER JOIN public.locations l ON l.location_id = lp.location_id
        CROSS JOIN center
        WHERE lp.geog IS NOT NULL
          AND ST_DWithin(lp.geog, center.g, radius_meters)
        ORDER BY lp.geog <-> center.g
        LIMIT result_limit
    ),
    primary_count AS (
        SELECT COUNT(*)::INT AS count FROM primary_set
    ),
    fill_candidates AS (
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
            (ST_Distance(l.geog, center.g) / 1000.0)::FLOAT AS distance_km,
            0.0::NUMERIC AS app_engagement_score,
            compute_google_baseline_score(l.rating, l.user_ratings_total) AS google_baseline_score,
            0.0::NUMERIC AS video_insight_score,
            0::INTEGER AS share_count,
            FALSE AS has_app_signal,
            0.0::NUMERIC AS quality_bias
        FROM public.locations l
        CROSS JOIN center
        CROSS JOIN primary_count pc
        WHERE l.geog IS NOT NULL
          AND pc.count < result_limit
          AND ST_DWithin(l.geog, center.g, radius_meters)
          AND NOT EXISTS (
              SELECT 1 FROM public.location_popularity_app lp
              WHERE lp.location_id = l.location_id
          )
        ORDER BY l.geog <-> center.g
        LIMIT result_limit
    ),
    fill_ranked AS (
        SELECT
            fc.*,
            ROW_NUMBER() OVER (ORDER BY fc.distance_km ASC, fc.location_id ASC) AS rn
        FROM fill_candidates fc
    ),
    fill_set AS (
        SELECT
            fr.location_id,
            fr.name,
            fr.vicinity,
            fr.lat,
            fr.lng,
            fr.cuisine,
            fr.cuisine_primary,
            fr.rating,
            fr.user_ratings_total,
            fr.price_level,
            fr.google_place_id,
            fr.types,
            fr.emoji,
            fr.vibe_vector,
            fr.dietary_requirement_vector,
            fr.distance_km,
            fr.app_engagement_score,
            fr.google_baseline_score,
            fr.video_insight_score,
            fr.share_count,
            fr.has_app_signal,
            fr.quality_bias
        FROM fill_ranked fr
        CROSS JOIN primary_count pc
        WHERE fr.rn <= GREATEST(result_limit - pc.count, 0)
    )
    SELECT *
    FROM (
        SELECT * FROM primary_set
        UNION ALL
        SELECT * FROM fill_set
    ) combined
    ORDER BY has_app_signal DESC, distance_km ASC;
$$;


-- ============================================================================
-- 4. HARD-DEPRECATE get_locations_with_quality
-- ============================================================================
-- All Python callers move to get_locations_with_pillars. The legacy RPC is
-- dropped so any code path still calling it fails loudly instead of
-- silently returning a stripped-down result.

DROP FUNCTION IF EXISTS get_locations_with_quality(
    DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, INT
);


-- ============================================================================
-- 5. DROP LEGACY compute_app_quality_score
-- ============================================================================
-- Replaced by the per-pillar functions (compute_app_engagement_score,
-- compute_google_baseline_score, compute_video_insight_score). The v6 cron
-- doesn't call this anymore.

DROP FUNCTION IF EXISTS compute_app_quality_score(
    INTEGER, INTEGER, INTEGER, NUMERIC, INTEGER, REAL, NUMERIC
);


-- ============================================================================
-- 6. PERMISSIONS
-- ============================================================================

GRANT EXECUTE ON FUNCTION refresh_location_quality_scores() TO service_role;
GRANT EXECUTE ON FUNCTION get_locations_with_pillars(
    DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, INT
) TO authenticated, anon, service_role;


-- ============================================================================
-- 7. RE-SEED IMMEDIATELY
-- ============================================================================
-- Populate pillar columns for the new cron's target set without waiting
-- until the next nightly run.

SELECT refresh_location_quality_scores();
