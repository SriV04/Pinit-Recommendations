# How to Find Google Place IDs

Google Place IDs are unique identifiers for places in Google's database. Here are several ways to find them:

## Method 1: Google Maps (Easiest)

1. Go to [Google Maps](https://maps.google.com)
2. Search for a restaurant/location
3. Click on the location to open its details
4. Look at the URL - the Place ID is in the URL after `!1s`
   
   Example URL:
   ```
   https://www.google.com/maps/place/Dishoom/@51.5369778,-0.1258446,17z/data=!3m1!4b1!4m6!3m5!1s0x48761b7d8fa9a941:0xf332443086561aab!8m2!3d51.5369778!4d-0.1232697!16s%2Fg%2F1tdsg75h
   ```
   
   The Place ID might not be directly visible in the URL, so use Method 2 or 3.

## Method 2: Place ID Finder Tool

1. Go to [Google's Place ID Finder](https://developers.google.com/maps/documentation/javascript/examples/places-placeid-finder)
2. Search for the location
3. Click on the marker
4. The Place ID will be displayed

## Method 3: Using Browser Console

1. Open Google Maps and search for a location
2. Right-click on the page and select "Inspect" (or press F12)
3. Go to the Console tab
4. Run this JavaScript:
   ```javascript
   document.querySelector('meta[itemprop="identifier"]')?.content
   ```
5. The Place ID will be displayed

## Method 4: Google Places API (Programmatic)

Use the Places API Text Search or Nearby Search:

```python
import requests
import os

def find_place_id(query: str, api_key: str):
    """Find Place ID by name/address"""
    url = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
    params = {
        "input": query,
        "inputtype": "textquery",
        "fields": "place_id,name,formatted_address",
        "key": api_key
    }
    
    response = requests.get(url, params=params)
    data = response.json()
    
    if data.get("candidates"):
        return data["candidates"][0]
    return None

# Example
api_key = os.getenv("GOOGLE_MAPS_API_KEY")
result = find_place_id("Dishoom King's Cross London", api_key)
print(f"Place ID: {result['place_id']}")
print(f"Name: {result['name']}")
```

## Example Place IDs (London Restaurants)

Here are some example Place IDs you can use for testing:

- **Dishoom King's Cross**: `ChIJQSa9d40bdkgRqxpWYKhEMvM`
- **The Ivy**: `ChIJf5khE0ocdkgRqvSjqw7vBCg`
- **Sketch**: `ChIJv3HKlEocdkgRSJZ8lqN6EWg`
- **Hoppers Soho**: `ChIJb-IaoFAcdkgR8kKvQ-XdVu0`
- **Dishoom Covent Garden**: `ChIJP8w-K04cdkgR3wlZ4h8TZ7E`

## Place ID Format

- Place IDs are alphanumeric strings
- Format: Typically start with `ChIJ` followed by random characters
- Example: `ChIJQSa9d40bdkgRqxpWYKhEMvM`
- They are permanent and don't change

## Using Place IDs with the API

Once you have a Place ID, you can add the location to your database:

```bash
curl -X POST http://localhost:8000/locations/add \
  -H "Content-Type: application/json" \
  -d '{"google_place_id": "ChIJQSa9d40bdkgRqxpWYKhEMvM"}'
```

Or with Python:

```python
import requests

response = requests.post(
    "http://localhost:8000/locations/add",
    json={"google_place_id": "ChIJQSa9d40bdkgRqxpWYKhEMvM"}
)

print(response.json())
```

## Important Notes

- Place IDs are stable and don't change over time
- A single place can have multiple IDs if it's listed in multiple ways
- Always validate that the Place ID returns a restaurant/food establishment
- Free tier of Google Places API has usage limits (check Google Cloud Console)

## Resources

- [Google Places API Documentation](https://developers.google.com/maps/documentation/places/web-service)
- [Place ID Overview](https://developers.google.com/maps/documentation/places/web-service/place-id)
- [Place ID Finder Tool](https://developers.google.com/maps/documentation/javascript/examples/places-placeid-finder)
