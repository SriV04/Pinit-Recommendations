ALTER TABLE public.locations ADD COLUMN IF NOT EXISTS photo_source text;
COMMENT ON COLUMN public.locations.photo_source IS 'Source of stored photo: og_image or google';
