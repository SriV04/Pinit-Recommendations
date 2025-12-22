# Proximal Recommendations API

REST API for location-based personalized restaurant recommendations.

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Ensure your `.env` file has Supabase credentials:
```
SUPABASE_URL=your_url
SUPABASE_SERVICE_ROLE_KEY=your_key
```

3. Start the API:
```bash
python start_api.py
```

The API will be available at `http://localhost:8000`

## API Endpoints

### Health Check
```bash
GET /health
```

### Get Proximal Recommendations
Get personalized recommendations within a radius:

```bash
POST /recommendations/proximal
Content-Type: application/json

{
  "user_id": "demo_date_night",
  "latitude": 51.5130,
  "longitude": -0.1240,
  "radius_km": 2.0,
  "max_results": 20,
  "taste_weight": 0.2,
  "proximity_weight": 0.6,
  "quality_weight": 0.2
}
```

**Response:**
```json
{
  "user_id": "demo_date_night",
  "center_lat": 51.5130,
  "center_lon": -0.1240,
  "radius_km": 2.0,
  "total_results": 20,
  "recommendations": [
    {
      "location_id": 123,
      "name": "Restaurant Name",
      "vicinity": "Address",
      "cuisine_primary": "italian",
      "rating": 4.5,
      "user_ratings_total": 500,
      "price_level": 2,
      "distance_km": 0.5,
      "taste_score": 0.85,
      "proximity_score": 0.95,
      "quality_score": 0.88,
      "final_score": 0.89,
      "rank": 1
    }
  ],
  "timestamp": "2025-12-22T10:30:00"
}
```

### Batch Recommendations
Get recommendations for multiple users:

```bash
POST /recommendations/proximal/batch
Content-Type: application/json

{
  "user_ids": ["demo_date_night", "demo_vegan"],
  "latitude": 51.5130,
  "longitude": -0.1240,
  "radius_km": 2.0,
  "max_results": 10
}
```

### Get Location Coordinates
```bash
GET /locations/{location_id}/coordinates
```

### List Users
```bash
GET /users?limit=10
```

### Get User Profile
```bash
GET /users/{user_id}/profile?top_n=10
```

## Interactive Documentation

FastAPI provides automatic interactive documentation:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Example Usage with curl

```bash
# Health check
curl http://localhost:8000/health

# Get recommendations
curl -X POST http://localhost:8000/recommendations/proximal \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "demo_date_night",
    "latitude": 51.5130,
    "longitude": -0.1240,
    "radius_km": 2.0,
    "max_results": 10
  }'

# List users
curl http://localhost:8000/users

# Get user profile
curl http://localhost:8000/users/demo_date_night/profile
```

## Example with Python

```python
import requests

# Get recommendations
response = requests.post(
    "http://localhost:8000/recommendations/proximal",
    json={
        "user_id": "demo_date_night",
        "latitude": 51.5130,
        "longitude": -0.1240,
        "radius_km": 2.0,
        "max_results": 20
    }
)

data = response.json()
print(f"Found {data['total_results']} recommendations")

for rec in data['recommendations'][:5]:
    print(f"{rec['rank']}. {rec['name']}")
    print(f"   Distance: {rec['distance_km']:.2f} km")
    print(f"   Score: {rec['final_score']:.2f}")
```

## Configuration

### Weights
You can adjust the scoring weights:
- `taste_weight`: User preference matching (default: 0.2)
- `proximity_weight`: Distance from center (default: 0.6)
- `quality_weight`: Rating and reviews (default: 0.2)

Weights should sum to 1.0 for best results.

### Radius
- Default: 2.0 km
- Maximum: 50 km
- API will auto-expand if insufficient results

## Performance

- Data is loaded once on startup and cached in memory
- First request after startup may take 5-10 seconds
- Subsequent requests are fast (~50-200ms)
- Suitable for production with proper caching strategies
