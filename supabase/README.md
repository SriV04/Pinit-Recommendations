# Supabase schema lives in `login`

This repo no longer owns database migrations. The production schema's single
source of truth is `login/supabase/migrations/` (baseline `20261001000000`).

Need a schema change for the recommender (new RPC, column, index)? Open a
migration in the login repo with `supabase migration new <name>`.

`_archive/migrations/` is history only — it is not replayed and is out of
sync with production.
