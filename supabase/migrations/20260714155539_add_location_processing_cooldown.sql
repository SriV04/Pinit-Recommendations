alter table public.locations
  add column if not exists location_processing_queued_at timestamptz,
  add column if not exists location_processing_claim_id text,
  add column if not exists location_processing_claimed_at timestamptz;

create index if not exists idx_locations_processing_queued_at
  on public.locations (location_processing_queued_at)
  where location_processing_queued_at is not null;

create index if not exists idx_locations_processing_claimed_at
  on public.locations (location_processing_claimed_at)
  where location_processing_claimed_at is not null;

create or replace function public.claim_location_processing(
  p_location_id integer,
  p_request_id text,
  p_cooldown_seconds integer default 2592000,
  p_claim_stale_after_seconds integer default 300
)
returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
  did_claim boolean := false;
begin
  update public.locations
  set
    location_processing_claim_id = p_request_id,
    location_processing_claimed_at = now()
  where location_id = p_location_id
    and (
      location_processing_queued_at is null
      or location_processing_queued_at <=
        now() - make_interval(secs => p_cooldown_seconds)
    )
    and (
      location_processing_claimed_at is null
      or location_processing_claimed_at <=
        now() - make_interval(secs => p_claim_stale_after_seconds)
    )
  returning true into did_claim;

  return coalesce(did_claim, false);
end;
$$;

create or replace function public.complete_location_processing_queue(
  p_location_id integer,
  p_request_id text
)
returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
  did_complete boolean := false;
begin
  update public.locations
  set
    location_processing_queued_at = now(),
    location_processing_claim_id = null,
    location_processing_claimed_at = null
  where location_id = p_location_id
    and location_processing_claim_id = p_request_id
  returning true into did_complete;

  return coalesce(did_complete, false);
end;
$$;

create or replace function public.release_location_processing_claim(
  p_location_id integer,
  p_request_id text
)
returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
  did_release boolean := false;
begin
  update public.locations
  set
    location_processing_claim_id = null,
    location_processing_claimed_at = null
  where location_id = p_location_id
    and location_processing_claim_id = p_request_id
  returning true into did_release;

  return coalesce(did_release, false);
end;
$$;

revoke all on function public.claim_location_processing(
  integer,
  text,
  integer,
  integer
) from public, anon, authenticated;
grant execute on function public.claim_location_processing(
  integer,
  text,
  integer,
  integer
) to service_role;

revoke all on function public.complete_location_processing_queue(integer, text)
  from public, anon, authenticated;
grant execute on function public.complete_location_processing_queue(integer, text)
  to service_role;

revoke all on function public.release_location_processing_claim(integer, text)
  from public, anon, authenticated;
grant execute on function public.release_location_processing_claim(integer, text)
  to service_role;
