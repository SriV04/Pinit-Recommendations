# User Tag Affinities - Supabase Integration

This document explains how user tag affinities are stored in Supabase and integrated with the recommendation system.

## Overview

User tag affinities represent a user's preferences for different location attributes (tags). The system now supports loading these affinities directly from Supabase's `user_tag_affinities` table.

## Database Schema

### user_tag_affinities Table

```sql
CREATE TABLE public.user_tag_affinities (
  user_id uuid NOT NULL,
  tag_id uuid NOT NULL,
  affinity real NOT NULL CHECK (affinity >= 0 AND affinity <= 100),
  evidence jsonb,
  updated_at timestamp with time zone NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, tag_id),
  FOREIGN KEY (user_id) REFERENCES public.users(supabase_id),
  FOREIGN KEY (tag_id) REFERENCES public.tags(tag_id)
);
```

**Columns:**
- `user_id` (uuid): Foreign key to users table
- `tag_id` (uuid): Foreign key to tags table  
- `affinity` (real): User preference score from 0 to 100
- `evidence` (jsonb): Optional metadata about how affinity was calculated
- `updated_at` (timestamp): Auto-updated timestamp

## Data Flow

### 1. Loading from Supabase

The system automatically checks Supabase for user tag affinities:

```python
from recommendation.user_profiles import load_user_tag_affinities_from_supabase

# Load all user affinities
affinities = load_user_tag_affinities_from_supabase()

# Load for specific user
user_affinities = load_user_tag_affinities_from_supabase(user_id="uuid-here")

# Filter by minimum affinity threshold (0-100 scale)
strong_affinities = load_user_tag_affinities_from_supabase(min_affinity=50.0)
```

### 2. Format Conversion

Supabase data and the recommendation system both use the same 0-100 scale for affinity/score:

```python
from recommendation.user_profiles import convert_supabase_affinities_to_profile_format
from recommendation.tag_taxonomy import get_tags_dataframe

tags_df = get_tags_dataframe()
profile_format = convert_supabase_affinities_to_profile_format(affinities, tags_df)
```

**Input Format (Supabase):**
```python
{
  "user_id": "uuid",
  "tag_id": "uuid", 
  "affinity": 85.0,  # 0 to 100
  "evidence": {"raw_score": 42.5},
  "updated_at": "2025-12-25T10:00:00"
}
```

**Output Format (Recommendation System):**
```python
{
  "user_id": "uuid",
  "tag_id": "uuid",
  "tag_text": "italian",
  "score": 85.0,  # 0 to 100 (same as affinity)
  "metadata": '{"raw_score": 42.5}'
}
```

### 3. API Integration

The API automatically loads from Supabase on startup:

```python
# In proximal_api.py load_data() function:
supabase_affinities = load_user_tag_affinities_from_supabase()

if not supabase_affinities.empty:
    # Use Supabase data
    user_tags = convert_supabase_affinities_to_profile_format(...)
else:
    # Fall back to synthetic/file data
    user_tags, user_history = build_user_tag_affinities(...)
```

## Syncing Data to Supabase

### Using the Sync Script

Upload user tag affinities from local data to Supabase:

```bash
# Dry run (preview what would be uploaded)
python src/scripts/sync_user_affinities.py --dry-run

# Upload with synthetic user profiles
python src/scripts/sync_user_affinities.py --synthetic

# Upload real user data with custom batch size
python src/scripts/sync_user_affinities.py --batch-size 50

# Full command with all options
python src/scripts/sync_user_affinities.py \
  --synthetic \
  --batch-size 100 \
  --dry-run
```

**Script Features:**
- ✅ Loads user actions from local files
- ✅ Builds tag affinities based on user behavior
- ✅ Converts format for Supabase (score → affinity)
- ✅ Uploads in configurable batches
- ✅ Progress bar with tqdm
- ✅ Error handling and summary report
- ✅ Dry-run mode for validation

**Output Example:**
```
============================================================
User Tag Affinities Sync
============================================================
Total records: 75
Unique users: 3
Unique tags: 25
Batch size: 100
Dry run: False
============================================================

Uploading to Supabase...
Progress: 100%|████████████████████████| 1/1 [00:02<00:00]

============================================================
Upload Summary
============================================================
✓ Successful: 75
✗ Errors: 0
============================================================
```

### Manual Upload via Python

```python
from supabase_client.supabase_service import get_supabase_service

db = get_supabase_service()

# Upload single affinity
db.create_user_tag_affinity(
    user_id="user-uuid",
    tag_id="tag-uuid",
    affinity=85.0,  # 0-100 scale
    evidence={"source": "user_actions", "action_count": 12}
)

# Update existing affinity
db.update_user_tag_affinity(
    user_id="user-uuid",
    tag_id="tag-uuid",
    affinity=90.0  # 0-100 scale
)

# Query affinities
affinities = db.get_user_tag_affinities(
    user_id="user-uuid",
    min_affinity=50.0  # 0-100 scale
)
```

## Priority System

The recommendation system loads data in this priority order:

1. **Supabase** (if available) - Real user preferences from database
2. **CSV files** (if available) - Historical data exports  
3. **Synthetic profiles** (if enabled) - Demo/test data

## API Behavior

### Startup Sequence

```
1. API starts → load_data() called
2. Check Supabase for user_tag_affinities
3. If found:
   ✓ Load from Supabase
   ✓ Convert format
   ✓ Cache in memory
4. If not found:
   → Try CSV files
   → Generate synthetic (if enabled)
5. Data ready for recommendation requests
```

### Example Logs

**With Supabase Data:**
```
Loading recommendation data...
Checking for user tag affinities in Supabase...
✓ Loaded 75 user tag affinities from Supabase
✓ Found 3 users in Supabase
✓ Loaded 5,000 locations
✓ Loaded 250 tags
✓ Loaded 75 user-tag affinities
✓ Data ready for API requests
```

**Fallback to Synthetic:**
```
Loading recommendation data...
Checking for user tag affinities in Supabase...
No user tag affinities found in Supabase, falling back to synthetic/file data...
✓ Generated synthetic user profiles
✓ Loaded 5,000 locations
✓ Loaded 250 tags
✓ Loaded 75 user-tag affinities
✓ Data ready for API requests
```

## Recommendation Integration

Once loaded, user tag affinities are used for taste scoring:

```python
from recommendation.proximal_recommendation import build_proximal_recommendations

recommendations = build_proximal_recommendations(
    user_id="demo_date_night",
    center_lat=51.5130,
    center_lon=-0.1240,
    locations=locations_df,
    location_tags=location_tags_df,
    user_tags=user_tags_df,  # From Supabase!
    config=ProximalConfig(
        radius_km=2.0,
        taste_weight=0.2,    # 20% user preference
        proximity_weight=0.6, # 60% distance
        quality_weight=0.2    # 20% rating
    )
)
```

## Data Quality

### Affinity Score Guidelines

- **0 - 20**: Weak/no interest
- **20 - 50**: Moderate interest  
- **50 - 80**: Strong preference
- **80 - 100**: Very strong preference

### Evidence Field Examples

```json
{
  "source": "user_actions",
  "action_count": 12,
  "recent_actions": 5,
  "action_types": {
    "save": 7,
    "like": 3,
    "detail_view": 2
  },
  "calculated_at": "2025-12-25T10:00:00Z"
}
```

## Monitoring

### Check Supabase Data

```sql
-- Count total affinities
SELECT COUNT(*) FROM user_tag_affinities;

-- Users with affinities
SELECT COUNT(DISTINCT user_id) FROM user_tag_affinities;

-- Top tags by average affinity
SELECT 
  t.text,
  AVG(uta.affinity) as avg_affinity,
  COUNT(*) as user_count
FROM user_tag_affinities uta
JOIN tags t ON t.tag_id = uta.tag_id
GROUP BY t.text
ORDER BY avg_affinity DESC
LIMIT 10;

-- User's top preferences
SELECT 
  t.text,
  uta.affinity
FROM user_tag_affinities uta
JOIN tags t ON t.tag_id = uta.tag_id
WHERE uta.user_id = 'uuid-here'
ORDER BY uta.affinity DESC
LIMIT 10;
```

### API Endpoint

Check loaded data via API:

```bash
# Health check shows user count
curl http://localhost:8000/health

# Get user profile
curl http://localhost:8000/users/{user_id}/profile
```

## Troubleshooting

### No data loading from Supabase

**Check:**
1. Environment variables set correctly (`.env`)
2. Supabase credentials valid
3. Table has data: `SELECT COUNT(*) FROM user_tag_affinities;`
4. Network connectivity to Supabase

### Format conversion errors

**Common issues:**
- Missing `text` column in tags table
- NULL tag_id values in affinities
- Invalid JSON in evidence field

### Sync script failures

**Solutions:**
- Run with `--dry-run` first to validate data
- Check batch size (reduce if timing out)
- Verify UUID format for user_id and tag_id
- Ensure tags exist in tags table before syncing
- Ensure affinity values are between 0-100

## Best Practices

1. **Regular Updates**: Update affinities as user behavior changes
2. **Evidence Tracking**: Include metadata about calcula10.0n method
3. **Threshold Filtering**: Only store affinities above 0.1 to reduce noise
4. **Batch Operations**: Use sync script for bulk updates
5. **Cache Strategy**: API caches on startup; restart to reload changes
6. **Monitoring**: Track affinity distribution and user coverage

## Performance Notes

- **Database Query**: ~50-100ms for all user affinities
- **Format Conversion**: ~10-20ms for 1000 records
- **API Startup**: 5-10 seconds with Supabase data
- **Memory Usage**: ~500KB per 1000 affinity records

## Future Enhancements

- [ ] Real-time affinity updates via webhooks
- [ ] Automatic re-calculation based on actions
- [ ] A/B testing different affinity weights
- [ ] Machine learning-based affinity prediction
- [ ] Collaborative filtering for new users
