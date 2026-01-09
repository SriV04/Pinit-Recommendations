import os
import base64
import requests
import time
from supabase import create_client, Client
from openai import OpenAI
from dotenv import load_dotenv


load_dotenv()
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_ANON_KEY"]
GOOGLE_API_KEY = os.environ["GOOGLE_PLACE_API_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
client = OpenAI(api_key=OPENAI_API_KEY)


# ---------------------------------------------------------
# Step 1 — Fetch google_place_id values from Supabase
# ---------------------------------------------------------
def get_all_google_place_ids():
    """Fetch all place IDs from locations table"""
    data = supabase.table("locations").select("google_place_id").execute()
    rows = data.data
    return [row["google_place_id"] for row in rows]


# ---------------------------------------------------------
# Step 2 — Fetch top photo reference for a Place ID
# ---------------------------------------------------------
def get_photo_reference(place_id: str):
    """Get the first photo resource name from Google Places API v1"""
    url = f"https://places.googleapis.com/v1/places/{place_id}"

    headers = {
        "Content-type" : "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": "id,displayName,photos"
    }

    try:
        res = requests.get(url, headers=headers, timeout=10).json()
        photos = res.get("photos", [])

        if not photos:
            return None

        # Extract photo resource name: "places/{place_id}/photos/{photo_id}"
        photo_name = photos[0].get("name")

        return photo_name  # Return the full resource name

    except Exception as e:
        print(f"Error fetching photo reference for {place_id}: {e}")
        return None


# ---------------------------------------------------------
# Step 3 — Download the actual image from Places Photo API
# ---------------------------------------------------------
def download_photo(photo_reference: str) -> bytes:
    """Download image bytes from Google Places Photo API"""

    url = f'https://places.googleapis.com/v1/{photo_reference}/media?maxHeightPx=400&maxWidthPx=400&key={GOOGLE_API_KEY}'
    

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.content  # raw JPEG bytes
    except Exception as e:
        return None


# ---------------------------------------------------------
# Step 4 — Send image to OpenAI vision model
# ---------------------------------------------------------
def classify_image_with_openai(image_bytes: bytes):
    """
    Send image to OpenAI vision model for classification.
    Returns a dict with classification results.
    """
    base64_img = base64.b64encode(image_bytes).decode("utf-8")

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",  # Fixed: use correct model name
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",  # Fixed: correct type
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_img}"
                            }
                        },
                        {
                            "type": "text",
                            "text": """# Role and Objective

You are a restaurant photo quality classifier. Your task is to evaluate a single restaurant photo and assign a quality score from 1-3 based on photographic professionalism. This score will be used to prioritize restaurants with high-quality cover photos in a recommendation system.

# Scoring System

- **3 (High Quality)**: Professional photography that represents the restaurant well
- **2 (Acceptable)**: Decent photo with some amateur qualities, but still usable
- **1 (Low Quality)**: Amateur smartphone photo that appears unprofessional

**Important**: Most professional restaurant photos should receive a 3. Only penalize clearly amateur or poorly executed photos. This is a filter for obviously bad content, not a strict professional photography critique.

# Evaluation Criteria

Assess the photo based on these factors:

## 1. Image Clarity and Resolution
- Is the image sharp and clear, or blurry and pixelated?
- Can you see details clearly, or is the image low-resolution?

## 2. Photography Quality
- Does this appear to be taken by a professional photographer or with professional equipment?
- Or does it look like a casual smartphone snapshot?

## 3. Composition
- Is the shot taken at a professional eye-level angle?
- Is the subject centered and well-framed?
- Does it show a full, complete view of the establishment/subject (not awkwardly cropped or partial)?

## 4. Lighting
- Is the lighting natural, well-balanced, and appropriate?
- Or is it dim, overexposed, or has harsh shadows/glare?

## 5. Staging and Presentation
- Does the photo appear intentionally composed and staged?
- Is the setting clean and presentable?


## Notes
Remember: You are filtering out clearly bad photos, not being a strict photography critic. When in doubt between scores, lean toward the higher score. Most professionally presented restaurants should receive a 3.

- **Be lenient**: If the photo meets professional standards across most criteria, assign a 3
- **Reserve score of 1** for clearly amateur photos with multiple significant issues (very blurry, terrible lighting, awkward angles, obvious smartphone snapshot quality)


# Reasoning Process

Before assigning your score, think step-by-step:

1. **Initial Assessment**: What is your first impression - does this look professional or amateur?
2. **Clarity Check**: Evaluate the resolution and sharpness
3. **Composition Analysis**: Assess the angle, framing, and completeness
4. **Lighting Evaluation**: Judge the lighting quality and balance
5. **Photography Style**: Determine if this appears professionally shot or like a casual smartphone photo
6. **Final Determination**: Based on the above, how many criteria pass professional standards?

## Irrelevant attributes
The following is irrelevant
- The **subject of the photo** (food, interior, exterior, ambiance - all are acceptable if professionally photographed)
- The **type of restaurant** (fine dining, casual, chains, local - judge only the photo quality)

# Scoring Guidelines

- **Score 3**: Professional photo where 4-5 criteria clearly pass professional standards
- **Score 2**: Acceptable photo where 2-3 criteria pass, but has some amateur qualities
- **Score 1**: Poor quality photo where 0-2 criteria pass, clearly unprofessional

# Output Format

Provide your response as exactly a single number of either 1,2,3:
e.g.
```
Score: 1
```

# Examples

## Example 1: Professional Restaurant Interior
**Image**: Well-lit dining room with elegant table settings, shot from eye level, centered composition, sharp focus, professionally staged
```
Score: 3
```

## Example 2: Amateur Smartphone Photo
**Image**: Blurry exterior shot taken at an upward angle, poor lighting with harsh shadows, partially cropped storefront, clearly taken quickly on a phone
```
Score: 1
```

## Example 3: Acceptable but Not Perfect
**Image**: Reasonably clear food photo with decent natural lighting, but slightly off-center composition and appears to be taken with a smartphone in good conditions
```
Score: 2
```
"""
                        }
                    ]
                },
            ],
            max_tokens=500
        )

        result_text = response.choices[0].message.content
        # Parse the score from the response
        # Expected format: "Score: [1, 2, 3]" or "Score: 2"
        import re
        score_match = re.search(r'Score:\s*(\d)', result_text)

        if score_match:
            score = int(score_match.group(1))
            return score
        else:
            # If no score found, return the raw text
            return None

    except Exception as e:
        print(f"Error classifying image with OpenAI: {e}")
        return {"error": str(e)}


# ---------------------------------------------------------
# Step 5 — Update Supabase with classification results
# ---------------------------------------------------------
def update_location_with_classification(place_id: str, to_update: dict):
    """Update the locations table with photo classification results"""
    try:
        # Debug: Check if the record exists first
        check = supabase.table("locations").select("google_place_id, location_id").eq("google_place_id", place_id).execute()

        if not check.data:
            print(f"✗ Record not found for google_place_id: {place_id}")
            return False

        # Perform update
        result = supabase.table("locations").update(to_update).eq("google_place_id", place_id).execute()

        if result.data:
            print(f"✓ Updated {place_id} with classification")
            return True
        else:
            print(f"✗ No rows updated for {place_id}")
            print(f"  Attempted update: {to_update}")
            return False
    except Exception as e:
        print(f"✗ Error updating {place_id}: {type(e).__name__}: {e}")
        print(f"  Attempted update: {to_update}")
        import traceback
        traceback.print_exc()
        return False


# ---------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------
def main():
    """
    Main pipeline:
    1. Fetch all place IDs from database
    2. For each place, get photo reference and download image
    3. Classify image with OpenAI vision model
    4. Save classification results to database
    """
    place_ids = get_all_google_place_ids()
    place_ids = place_ids[253:]
    total = len(place_ids)

    print(f"Processing {total} locations...")

    processed = 0
    skipped = 0
    errors = 0

    for idx, place_id in enumerate(place_ids, 1):
        print(f"\n[{idx}/{total}] Processing {place_id}...")

        # Always fetch photo reference from API v1
        print(f"  ↳ Fetching photo reference...")
        photo_ref = get_photo_reference(place_id)

        if not photo_ref:
            print(f"  ↳ No photo found")
            skipped += 1
            continue

        # Download image
        print(f"  ↳ Downloading photo...")
        image_bytes = download_photo(photo_ref)

        if not image_bytes:
            print(f"  ↳ Failed to download photo")
            errors += 1
            continue

        # Classify with OpenAI
        print(f"  ↳ Classifying with OpenAI...")
        classification = classify_image_with_openai(image_bytes)
        toUpdate = {'photo_reference': photo_ref, 'photo_reference_score':classification}
        print(toUpdate, 'with a id', place_id)
        # Update database
        success = update_location_with_classification(place_id, toUpdate)

        if success:
            processed += 1
        else:
            errors += 1

        # Rate limiting: sleep to avoid hitting API limits
        time.sleep(1)  # Adjust based on your API rate limits

    print(f"\n{'='*60}")
    print(f"Complete!")
    print(f"  Processed: {processed}")
    print(f"  Skipped: {skipped}")
    print(f"  Errors: {errors}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
