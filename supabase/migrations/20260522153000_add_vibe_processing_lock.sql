alter table public.locations
  add column if not exists vibe_processing_request_id text,
  add column if not exists vibe_processing_started_at timestamp with time zone;

create index if not exists idx_locations_vibe_processing_started_at
  on public.locations (vibe_processing_started_at)
  where vibe_processing_started_at is not null;

create or replace function public.claim_location_vibe_processing(
  p_location_id integer,
  p_request_id text,
  p_stale_after_seconds integer default 900
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  did_claim boolean := false;
begin
  update public.locations
  set
    vibe_processing_request_id = p_request_id,
    vibe_processing_started_at = now()
  where location_id = p_location_id
    and coalesce(updated_vibe, false) = false
    and (
      vibe_processing_started_at is null
      or vibe_processing_started_at < now() - make_interval(secs => p_stale_after_seconds)
    )
  returning true into did_claim;

  return coalesce(did_claim, false);
end;
$$;

grant execute on function public.claim_location_vibe_processing(integer, text, integer)
  to service_role;
