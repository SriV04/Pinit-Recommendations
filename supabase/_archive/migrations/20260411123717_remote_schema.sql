drop function if exists "public"."send_message"(p_bubble_id uuid, p_content text, p_message_type text, p_metadata jsonb, p_replied_to uuid);

drop function if exists "public"."get_bubble_messages"(p_bubble_id uuid, p_limit integer, p_before_timestamp timestamp with time zone);

drop function if exists "public"."get_locations_with_quality"(center_lat double precision, center_lng double precision, radius_meters double precision, result_limit integer);

alter type "public"."action_type" rename to "action_type__old_version_to_be_dropped";

create type "public"."action_type" as enum ('save', 'like', 'shared_video', 'dislike', 'bubble_save', 'been_to');


  create table "public"."location_similarities" (
    "location_id_a" bigint not null,
    "location_id_b" bigint not null,
    "similarity_score" double precision not null,
    "co_save_count" integer not null default 0,
    "updated_at" timestamp with time zone not null default now()
      );


alter table "public"."user_location_actions" alter column action type "public"."action_type" using action::text::"public"."action_type";

drop type "public"."action_type__old_version_to_be_dropped";

alter table "public"."location_popularity_app" add column "been_to_count" integer;

alter table "public"."location_popularity_app" add column "quality_score" numeric;

alter table "public"."location_reviews" alter column "rating" set data type numeric(3,1) using "rating"::numeric(3,1);

alter table "public"."messages" add column "location_id" bigint;

alter table "public"."users" alter column "dietary_requirement_tag_affinity" set data type real[] using "dietary_requirement_tag_affinity"::real[];

alter table "public"."users" alter column "vibe_tag_affinity" set data type real[] using "vibe_tag_affinity"::real[];

CREATE INDEX idx_loc_sim_a ON public.location_similarities USING btree (location_id_a);

CREATE INDEX idx_loc_sim_b ON public.location_similarities USING btree (location_id_b);

CREATE INDEX idx_messages_location_id ON public.messages USING btree (location_id) WHERE (location_id IS NOT NULL);

CREATE UNIQUE INDEX location_similarities_pkey ON public.location_similarities USING btree (location_id_a, location_id_b);

alter table "public"."location_similarities" add constraint "location_similarities_pkey" PRIMARY KEY using index "location_similarities_pkey";

alter table "public"."location_similarities" add constraint "location_similarities_location_id_a_fkey" FOREIGN KEY (location_id_a) REFERENCES public.locations(location_id) not valid;

alter table "public"."location_similarities" validate constraint "location_similarities_location_id_a_fkey";

alter table "public"."location_similarities" add constraint "location_similarities_location_id_b_fkey" FOREIGN KEY (location_id_b) REFERENCES public.locations(location_id) not valid;

alter table "public"."location_similarities" validate constraint "location_similarities_location_id_b_fkey";

alter table "public"."messages" add constraint "messages_location_id_fkey" FOREIGN KEY (location_id) REFERENCES public.locations(location_id) ON DELETE SET NULL not valid;

alter table "public"."messages" validate constraint "messages_location_id_fkey";

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.compute_quality_score_v3(p_rating double precision, p_user_ratings_total numeric, p_saved_count smallint DEFAULT 0, p_saves_count_app integer DEFAULT 0, p_dislikes_count_app integer DEFAULT 0, p_recent_reviews_90d integer DEFAULT 0, p_total_app_reviews integer DEFAULT 0, p_location_age_days integer DEFAULT 365)
 RETURNS double precision
 LANGUAGE plpgsql
 IMMUTABLE
AS $function$DECLARE
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
END;$function$
;

CREATE OR REPLACE FUNCTION public.create_location_review(p_location_id bigint, p_content text DEFAULT NULL::text, p_rating numeric DEFAULT NULL::numeric, p_gatekeep boolean DEFAULT false)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
  v_user_id uuid;
  v_review  public.location_reviews%ROWTYPE;
BEGIN
  v_user_id := auth.uid();

  IF v_user_id IS NULL THEN
    RETURN jsonb_build_object('success', false, 'error', 'Not authenticated');
  END IF;

  INSERT INTO public.location_reviews (
    location_id,
    user_id,
    content,
    rating,
    private
  )
  VALUES (
    p_location_id,
    v_user_id,
    p_content,
    p_rating,
    p_gatekeep
  )
  RETURNING * INTO v_review;

  -- Record the been_to action (idempotent via ON CONFLICT UPDATE)
  PERFORM public.create_user_location_action(
    p_user_id      => v_user_id,
    p_location_id  => p_location_id,
    p_action       => 'been_to'
  );

  RETURN jsonb_build_object(
    'success', true,
    'id',      v_review.id
  );

EXCEPTION
  WHEN OTHERS THEN
    RETURN jsonb_build_object('success', false, 'error', SQLERRM);
END;
$function$
;

CREATE OR REPLACE FUNCTION public.create_new_collection(p_name text, p_description text, p_emoji text, p_cover_color text, p_user_id uuid, p_is_public boolean)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
    new_collection_id uuid;
BEGIN
    INSERT INTO collections (name, description, emoji, cover_color, created_by, is_curated, is_public, created_at)
    VALUES (p_name, p_description, p_emoji, p_cover_color, p_user_id, true, p_is_public, now())
    RETURNING collection_id INTO new_collection_id;

    RETURN new_collection_id;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_bubble_members(p_bubble_id uuid)
 RETURNS TABLE(user_id uuid, supabase_id uuid, name text, profile_image_url text)
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM public.bubble_members bm
    WHERE bm.bubble_id = p_bubble_id
      AND bm.user_id = auth.uid()
  ) THEN
    RAISE EXCEPTION 'User not a member of this bubble';
  END IF;

  RETURN QUERY
  SELECT
    bm.user_id,
    u.supabase_id,
    u.name,
    u.profile_image_url
  FROM public.bubble_members bm
  INNER JOIN public.users u
    ON u.supabase_id = bm.user_id
  WHERE bm.bubble_id = p_bubble_id
  ORDER BY bm.created_at ASC;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_location_collection_ids(p_location_id bigint)
 RETURNS TABLE(collection_id uuid)
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
BEGIN
  RETURN QUERY
  SELECT cl.collection_id
  FROM public.collection_locations cl
  INNER JOIN public.collections c ON c.collection_id = cl.collection_id
  WHERE cl.location_id = p_location_id
    AND c.created_by = auth.uid();
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_user_been_to_count(p_user_id uuid)
 RETURNS integer
 LANGUAGE sql
 STABLE SECURITY DEFINER
AS $function$
  SELECT count(*)::integer
  FROM public.user_location_actions
  WHERE user_id = p_user_id
    AND action = 'been_to';
$function$
;

CREATE OR REPLACE FUNCTION public.get_user_been_to_reviews(p_user_id uuid)
 RETURNS TABLE(review_id uuid, location_id bigint, location_name text, rating numeric, content text, private boolean, created_at timestamp with time zone)
 LANGUAGE sql
 STABLE SECURITY DEFINER
AS $function$
  SELECT
    lr.id            AS review_id,
    lr.location_id,
    l.name           AS location_name,
    lr.rating,
    lr.content,
    lr.private,
    lr.created_at
  FROM public.location_reviews lr
  JOIN public.locations l ON l.location_id = lr.location_id
  JOIN public.user_location_actions ula
    ON ula.user_id    = lr.user_id
   AND ula.location_id = lr.location_id
   AND ula.action      = 'been_to'
  WHERE lr.user_id = p_user_id
  ORDER BY lr.created_at DESC;
$function$
;

CREATE OR REPLACE FUNCTION public.send_message(p_bubble_id uuid, p_content text, p_message_type text DEFAULT 'text'::text, p_metadata jsonb DEFAULT NULL::jsonb, p_replied_to uuid DEFAULT NULL::uuid, p_location_id bigint DEFAULT NULL::bigint)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
    v_message_id UUID;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM bubble_members
        WHERE bubble_id = p_bubble_id
        AND user_id = auth.uid()
    ) THEN
        RAISE EXCEPTION 'User not a member of this bubble';
    END IF;

    INSERT INTO messages (
        bubble_id,
        sender_id,
        content,
        message_type,
        metadata,
        replied_to_message_id,
        location_id
    )
    VALUES (
        p_bubble_id,
        auth.uid(),
        p_content,
        p_message_type,
        p_metadata,
        p_replied_to,
        p_location_id
    )
    RETURNING id INTO v_message_id;

    INSERT INTO user_chat_state (user_id, bubble_id, last_read_at)
    VALUES (auth.uid(), p_bubble_id, NOW())
    ON CONFLICT (user_id, bubble_id)
    DO UPDATE SET last_read_at = NOW();

    RETURN v_message_id;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.update_collection(p_collection_id uuid, p_name text, p_cover_color text)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
begin
  update collections
  set
    name        = coalesce(p_name, name),
    cover_color = p_cover_color
  where collection_id = p_collection_id
    and created_by = auth.uid();
end;
$function$
;

CREATE OR REPLACE FUNCTION public.get_bubble_messages(p_bubble_id uuid, p_limit integer DEFAULT 50, p_before_timestamp timestamp with time zone DEFAULT NULL::timestamp with time zone)
 RETURNS TABLE(id uuid, sender_id uuid, sender_name text, sender_avatar_url text, content text, message_type text, metadata jsonb, created_at timestamp with time zone, updated_at timestamp with time zone, replied_to_message_id uuid, location_id bigint)
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM bubble_members
        WHERE bubble_id = p_bubble_id
        AND user_id = auth.uid()
    ) THEN
        RAISE EXCEPTION 'User not a member of this bubble';
    END IF;

    RETURN QUERY
    SELECT
        m.id,
        m.sender_id,
        u.name as sender_name,
        u.profile_image_url as sender_avatar_url,
        m.content,
        m.message_type,
        m.metadata,
        m.created_at,
        m.updated_at,
        m.replied_to_message_id,
        m.location_id
    FROM messages m
    JOIN users u ON m.sender_id = u.supabase_id
    WHERE m.bubble_id = p_bubble_id
    AND m.is_deleted = false
    AND (p_before_timestamp IS NULL OR m.created_at < p_before_timestamp)
    ORDER BY m.created_at DESC
    LIMIT p_limit;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_locations_with_quality(center_lat double precision, center_lng double precision, radius_meters double precision, result_limit integer DEFAULT 100000)
 RETURNS TABLE(location_id bigint, name text, vicinity text, lat numeric, lng numeric, created_at timestamp without time zone, cuisine text, rating real, user_ratings_total numeric, price_level numeric, photo_reference text, saved_count smallint, google_place_id text, business_status text, editorial_summary text, website text, international_phone_number text, types text, opening_hours_text text[], opening_hours_periods jsonb, open_now boolean, cuisine_detected text, cuisine_source text, cuisine_primary text, review_language_counts_json jsonb, is_open_late boolean, is_open_early boolean, is_sunday_open boolean, price_bucket text, derived_attributes jsonb, data_version text, ingested_at timestamp without time zone, emoji text, vibe_vector integer[], dietary_requirement_vector integer[], distance_km double precision, quality_score double precision)
 LANGUAGE plpgsql
 STABLE
AS $function$
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
$function$
;

grant delete on table "public"."location_similarities" to "anon";

grant insert on table "public"."location_similarities" to "anon";

grant references on table "public"."location_similarities" to "anon";

grant select on table "public"."location_similarities" to "anon";

grant trigger on table "public"."location_similarities" to "anon";

grant truncate on table "public"."location_similarities" to "anon";

grant update on table "public"."location_similarities" to "anon";

grant delete on table "public"."location_similarities" to "authenticated";

grant insert on table "public"."location_similarities" to "authenticated";

grant references on table "public"."location_similarities" to "authenticated";

grant select on table "public"."location_similarities" to "authenticated";

grant trigger on table "public"."location_similarities" to "authenticated";

grant truncate on table "public"."location_similarities" to "authenticated";

grant update on table "public"."location_similarities" to "authenticated";

grant delete on table "public"."location_similarities" to "service_role";

grant insert on table "public"."location_similarities" to "service_role";

grant references on table "public"."location_similarities" to "service_role";

grant select on table "public"."location_similarities" to "service_role";

grant trigger on table "public"."location_similarities" to "service_role";

grant truncate on table "public"."location_similarities" to "service_role";

grant update on table "public"."location_similarities" to "service_role";


  create policy "location_reviews_delete"
  on "public"."location_reviews"
  as permissive
  for delete
  to authenticated
using ((user_id = auth.uid()));



  create policy "location_reviews_insert"
  on "public"."location_reviews"
  as permissive
  for insert
  to authenticated
with check ((user_id = auth.uid()));



  create policy "location_reviews_select"
  on "public"."location_reviews"
  as permissive
  for select
  to authenticated
using (((user_id = auth.uid()) OR (private = false)));



  create policy "location_reviews_update"
  on "public"."location_reviews"
  as permissive
  for update
  to authenticated
using ((user_id = auth.uid()))
with check ((user_id = auth.uid()));



  create policy "Collection covers are publicly readable"
  on "storage"."objects"
  as permissive
  for select
  to public
using ((bucket_id = 'collection_covers'::text));



  create policy "Users can update collection covers"
  on "storage"."objects"
  as permissive
  for update
  to authenticated
using ((bucket_id = 'collection_covers'::text));



  create policy "Users can upload collection covers"
  on "storage"."objects"
  as permissive
  for insert
  to authenticated
with check ((bucket_id = 'collection_covers'::text));



