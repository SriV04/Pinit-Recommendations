"""
Restaurant Vibe Tag Generator Service
=======================================
Uses a 3-stage xAI Grok pipeline to analyze restaurant data and generate
vibe tag scores (vibe_vector).

Ported from login/ai/vibe-tags/generate_vibe_tags.py for use within the
Pinit-Recommendations API.

Pipeline:
  Stage 1: Identify relevant and irrelevant tags with evidence
  Stage 2: Score relevant tags (60-100)
  Stage 3: Score irrelevant tags (10-50)
  Combine into vibe_vector and update Supabase.

Usage:
    await generate_vibe_tags_for_location(location_id=123)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional

from openai import AsyncOpenAI

from pinit.config.secrets import XAI_API_KEY
from pinit.integrations.supabase import get_supabase_service

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

XAI_BASE_URL = "https://api.x.ai/v1"
MODEL = "grok-4-fast-non-reasoning"

VIBE_TAGS_ORDERED = [
    "cafe", "casual", "cozy", "coffee_shop", "bar",
    "elegant", "fine_dining", "food_truck", "hole_in_the_wall", "late_night",
    "live_music", "modern", "fast_food", "romantic", "sports_bar",
    "takeout_friendly", "pub", "grocery_store", "brunch", "outdoor_dining",
    "wavy", "bossman",
]

# ── Prompts ──────────────────────────────────────────────────────────────────
# Embedded from login/ai/vibe-tags/*.md prompt files.

VALID_TAG_PROMPT = r"""# Role and Objective

You are a restaurant vibe analyst. Given structured restaurant data, determine which of the "vibe" tags genuinely apply to the restaurant and provide brief supporting evidence for each.

NOTE: This is used as a part of a two-step process, the output produced from this will be consumed by a scoring tool to quantify by how much each of the following tags  apply/don't apply


# Vibe Tag Definitions

Each tag has a specific meaning. Only select a tag if the restaurant data provides real evidence for it — do not select tags based on loose associations.

Use the "Key Distinguishing Signals" column as a first point of reference but not a exhaustive list of conditions that the tag must satisfy.

| Tag | Definition | Key Distinguishing Signals |
|-----|-----------|---------------------------|
| `cafe` | A primarily daytime establishment centered around beverages like coffee and tea and light food. Differs from coffee_shop as more food/bakery options but also not just a restaurant that serves coffee. | `types` includes "cafe"; light meal focus; daytime hours dominant |
| `casual` | Relaxed, low-pressure atmosphere where dress code and formality are minimal. | Review language like "laid-back", "no fuss", "come as you are"; moderate or budget pricing |
| `cozy` | Physically small or warm-feeling space that creates intimacy or comfort. Not the same as quiet or romantic. | Reviews mentioning "cozy", "warm", "snug", "homey"; small seating capacity; fireplace/candles |
| `coffee_shop` | Primary business is serving coffee and coffee-based drinks. Distinct from `cafe` in that food is secondary or minimal. | `serves_coffee: true` AND `types` suggests coffee-first; limited food menu |
| `bar` | Venues primarilly in the evenings focused on serving alcoholioc drinks and good vibes. Not just any place that serves alcohol" | Attributes to do with alcohol such as `serves_cocktails: true`, `serves_beer: true`,  reviews/summaries mentioning alcohol, late night, music |
| `elegant` | Refined atmosphere with attention to aesthetic detail — decor, plating, service style. Exists primarily at high price points. Distinct from `fine_dining` (complete premium experience) | Reviews mentioning "beautiful", "elegant", "stunning decor", "sophisticated"; premium food options wagyu, caviar, lobster etc. |
| `fine_dining` | Full premium experience: high price, formal service, tasting menus or chef-driven cuisine, reservations expected. | `price_bucket` is "luxury" or "expensive"; tasting menu mentions; formal service language |
| `food_truck` | Mobile food vendor or permanent establishment with food-truck origins/style. | `types` includes food truck indicators; street food style; very limited menu |
| `hole_in_the_wall` | Small, unassuming, no-frills establishment where food quality dramatically outperforms ambiance. | Budget pricing with high rating; reviews contrasting humble appearance with great food; "hidden gem" language |
| `late_night` | Open significantly past typical dinner hours (past 11 PM), and this is a notable part of the experience. | `is_open_late: true`; hours confirm late closing; reviews mention late-night visits |
| `live_music` | Regular live music performances are part of the experience, not just background playlist. | `live_music: true`; reviews mention performances, bands, musicians |
| `modern` | Contemporary approach to cuisine, decor, or concept. Fusion, reinterpretation of traditional dishes, or distinctly current aesthetic. | "Modern", "contemporary", "innovative", "fusion" in text; non-traditional takes on cuisine |
| `fast_food` | Fast service, minimal wait, designed for eating in under 30 minutes. Distinct from `casual` (atmosphere, not speed). | Counter service; fast food adjacent; takeout-heavy; small portions; budget price |
| `romantic` | Suitable for dates and romantic occasions due to atmosphere, lighting, intimacy, or service style. | "Romantic", "date night", "intimate", "candlelit" in reviews; dim lighting; small tables for two |
| `sports_bar` | Establishment where watching sports is a primary draw — TV screens, dart boards / pool tables (pub games of sort), fan atmosphere. | `good_for_watching_sports: true`; reviews mention TVs, game day, sports |
| `takeout_friendly` | Well-suited for takeout or delivery — food travels well, ordering is easy. | Reviews mention takeout/delivery positively; `types` includes delivery/meal_takeout; quick service format |
| `pub` | A traditional pub or gastropub where beer, ale, or cider is central to the identity. Community-oriented, often with hearty food. Distinct from `bar` (pub implies warmth, tradition, food; bar implies nightlife, cocktails, vibes). | `serves_beer: true` AND reviews/summaries mention "pub", "ales", "pints", "gastropub", "pub grub"; British/Irish character; relaxed communal atmosphere |
| `grocery_store` | Establishment primarily selling ingredients or ready-made food items for customers to take home, as opposed to preparing and serving meals on-site. | `types` includes "grocery_or_supermarket", "supermarket"; reviews mention "shopping", "ingredients", "products", "shelves"; no dine-in language;|
| `brunch` | Establishment where brunch is a speciality — typically weekend daytime service with a dedicated brunch menu, bottomless drinks, or a social brunch atmosphere. Not just any restaurant that happens to be open on weekend mornings. | `serves_brunch: true` AND reviews/summaries mention "brunch", or specific breakfast/brunch items; dedicated brunch menu or brunch-specific offerings; daytime weekend hours |
| `outdoor_dining` | Establishment with notable outdoor seating that is a draw in itself — terraces, rooftop, balcony, garden, or pavement dining. Not just a single bench outside. The outdoor space should be a meaningful part of the experience. | `outdoor_seating: true` AND reviews/summaries mention "terrace", "rooftop", "garden", "al fresco", "outdoor", "patio", "balcony"; photos suggest substantial outdoor area |
| `wavy` | A place with a sense of novelty, cultural currency, or creative edge — it feels current and worth talking about. Not about price or formality, but about whether the concept, cuisine, or execution feels fresh rather than formulaic. Chains can be wavy if the concept still feels distinctive. | Reviews/summaries mention "unique", "creative", "interesting", "different", "must-try"; non-ubiquitous cuisine or novel concept; strong social media presence or word-of-mouth buzz; independent OR chain but not mass-market commoditized food has an identity rather than being purely functional |
| `bossman` | A functional eatery where the food is commoditised and interchangeable — the kind of place you go out of convenience or habit, not discovery. Covers generic high-street chicken shops, pizza shops and delis. The place should have no real brand or flavour identity. | Very cheap, often low quality food; Ubiquitous place with distinguishing concept; cuisine is mass-market commoditized, low-price point, quick and open-late|


# Input
You will receive a JSON object that contains attributes of the restaurant.

These fields are generated using the Google Places API and some data is partial. You should consider all sources however when all is avaialable some fields are more relevant than others. When present and non-null, boolean attributes provide strong direct signals. Text fields provide the richest contextual evidence and should be used to confirm or override boolean signals.

Use the following attributes in this way:

1. **Boolean attributes**: These are direct values obtained from the google places API that identify whether a restaurant has certain values. The specific fields that this attributes to and how they relate to tags is as follows:
  - `live_music` supports "live_music", "romantic" tag opposes "food_truck"
  - `serves_cocktails` supports "bar" opposes "food_truck"
  - `good_for_children` opposes "elegant", "fine_dining"
  - `good_for_watching_sports` strongly supports "sports_bar" opposes "elegant", "fine_dining"
  - `outdoor_seating` supports "casual", "outdoor_dining"
  - `serves_beer` supports "casual", "pub"
  - `serves_coffee` strongly supports "cafe"
  - `serves_wine` supports "bar" and "pub"
  - `serves_brunch` supports "brunch"
  - `is_open_late: true` strongly supports "late_night"
NOTE : Many of these values may not be populated and will be set to NULL. Do not assume any information from a NULL value, only make decisions if TRUE/FALSE


2. **Text fields**: Look for tone, vocabulary, and themes in `review_summary`, `generated_summary`, and `editorial_summary`

3. **Price & rating**: `price_bucket` and `rating` help distinguish upscale/fine-dining from casual/diner/hole-in-the-wall.

4. **Cuisine & types**: `cuisine_primary` and `types` provide baseline context (e.g., "cafe" type directly supports the "cafe" tag).


# Instructions

1. Read all input fields carefully. Treat each field as a potential source of evidence. however some tags will be more relevant than others:
2. For each of the 22 tags, determine if the restaurant data provides genuine evidence for it.
3. A tag is "relevant" if there is at least one concrete signal from the input data supporting it. Do not tag based on vibes-of-vibes — e.g., don't infer `romantic` just because a place is `elegant`.
4. If a field is `null` or missing, that is not evidence for or against any tag — simply ignore it.
5. Be honest about absence of evidence. It is better to return 3 genuinely supported tags than 10 speculative ones.

# Consistency Rules

- `fine_dining` and `hole_in_the_wall` are mutually exclusive.
- `fast_food` and `fine_dining` are mutually exclusive.
- `coffee_shop` should not be tagged alongside `fine_dining` or `pub` unless there is very strong evidence for both.
- If you find yourself selecting both sides of a contradiction, re-examine the evidence and drop the weaker one.

# Output Format

Return ONLY a valid JSON object with this structure:
```json
{
  "relevant_tags": [
    {
      "tag": "tag_name",
      "evidence": "Specific evidence from the input data as to why it was included. Fields supporting this are cited ."
    }
  ],
  "irrelevant_tags": [
    {
      "tag": "tag_name",
      "reason": "Specific eeason this nearby/plausible tag was excluded. Contradictory fields included"
    }
  ]
}
```

- `relevant_tags`: All tags with genuine supporting evidence. Cite which input fields support each tag.
- `irrelevant_tags`: All tags that had lack of evidence to be rated as positive. Cite direct contradictions or reasons for insufficent information

NOTE: Your evidence statements will be read by a downstream scoring model to assign numerical intensity scores (0-100) per tag. Write evidence that is specific and comprehensive enough for another model to distinguish between a weak and strong fit."

Do not include markdown fences, explanation text, or anything outside the JSON object."""


VALID_TAG_SCORER = r"""# Role and Objective

You are a scoring model for restaurant vibe tags. You will receive:
1. Complete restaurant data in JSON format
2. A list of tags that have been identified as **relevant** with supporting evidence

Your task is to assign a discrete score to each relevant tag. Assign each tag to one of five score buckets based on the strength, quantity, and centrality of the evidence.

# Vibe Tag Scoring Signals

| Tag | Definition | 60-70 Scoring Signals (supported but secondary) | 80-100 Scoring Signals (strong / core / defining) |
|-----|-----------|--------------------------------------------------|---------------------------------------------------|
| `cafe` | A primarily daytime establishment centered around light food and beverages. | `serves_coffee: true` and some light food options, but the restaurant also serves full dinner; `types` includes "cafe" but reviews focus on meals rather than cafe culture | `types` includes "cafe"; daytime hours dominant; reviews describe "coffee spot", "light lunch"; editorial summary leads with cafe identity; no significant dinner service |
| `casual` | Relaxed, low-pressure atmosphere where dress code and formality are minimal. | `outdoor_seating: true` or `price_bucket` is "moderate"/"budget", casualness is implied rather than stated | Multiple reviews use "laid-back", "no fuss", "relaxed", "come as you are"; `price_bucket` is "budget" or "moderate"; no formality in service |
| `cozy` | Physically small or warm-feeling space that creates intimacy or comfort. | mentions "intimate" or "warm" but the restaurant is primarily known for food or other vibes | Multiple reviews use "cozy", "warm", "snug", "homey"; fireplace or candles mentioned; small seating capacity confirmed |
| `coffee_shop` | Primary business is serving coffee and coffee-based drinks. | `serves_coffee: true` and reviews praise the coffee, but a substantial food menu exists alongside | reviews focus on coffee quality and variety; food menu is minimal or secondary; editorial summary centres on coffee |
| `bar` | Venues primarily in the evenings focused on serving alcoholic drinks and good vibes. | `serves_cocktails: true` and reviews mention drinks positively, but the restaurant is primarily a dining venue | Reviews focus on drinks atmosphere, nightlife energy, or cocktail quality; `is_open_late: true`; evening-dominant hours |
| `elegant` | Refined atmosphere with attention to aesthetic detail. | One review mentions "beautiful" or "nice decor", but atmosphere is not what the restaurant is primarily known for | Multiple reviews mention "elegant", "stunning", "sophisticated", "beautiful plating"; `price_bucket` is "expensive" or "luxury" |
| `fine_dining` | Full premium experience: high price, formal service, tasting menus or chef-driven cuisine. | `price_bucket` is "expensive" and service is attentive, but no tasting menu, no formal service language | `price_bucket` is "luxury"; reviews describe "tasting menu", "impeccable service", "reservations essential" |
| `food_truck` | Mobile food vendor or permanent establishment with food-truck origins/style. | `types` hints at street food; very limited menu; casual grab-and-go style but has some permanent seating | `types` explicitly includes food truck; reviews reference mobile vendor or truck; no dine-in experience |
| `hole_in_the_wall` | Small, unassuming, no-frills establishment where food quality dramatically outperforms ambiance. | Budget pricing and high rating, but reviews don't explicitly contrast appearance with food quality | Reviews use "hidden gem", "don't let the outside fool you", "unassuming"; budget pricing with notably high rating |
| `late_night` | Open significantly past typical dinner hours (past 11 PM). | `is_open_late: true` and hours confirm late closing, but no reviews mention late-night visits | `is_open_late: true`; hours confirm late closing most nights; reviews mention late-night visits |
| `live_music` | Regular live music performances are part of the experience. | `live_music: true` but no reviews mention specific performances | `live_music: true`; multiple reviews mention bands, performers, "great live music" |
| `modern` | Contemporary approach to cuisine, decor, or concept. | One review mentions "innovative" or decor feels contemporary, but the core cuisine is traditional | Multiple reviews use "modern", "innovative", "fusion", "contemporary" |
| `fast_food` | Fast service, minimal wait, designed for eating in under 20 minutes. | Counter service or fast turnaround mentioned, but the restaurant also has table service | Counter service dominant; reviews mention "fast", "in and out", "grab and go"; `price_bucket` is "budget" |
| `romantic` | Suitable for dates and romantic occasions. | Restaurant is described as "intimate" and has dim lighting, but only one review mentions dates | Multiple reviews use "date night", "romantic", "candlelit"; `good_for_children: false` |
| `sports_bar` | Establishment where watching sports is a primary draw. | `good_for_watching_sports: true` but reviews don't focus on sports | `good_for_watching_sports: true`; multiple reviews mention screens, game day, sports atmosphere |
| `takeout_friendly` | Well-suited for takeout or delivery. | `types` includes delivery/takeout, but no reviews discuss takeout quality | Reviews praise takeout specifically; packaging or delivery speed mentioned |
| `pub` | A traditional pub or gastropub where beer, ale, or cider is central. | `serves_beer: true` and the atmosphere is relaxed, but reviews don't use pub-specific language | Multiple reviews use "pub", "gastropub", "great ales", "pints"; `serves_beer: true` |
| `grocery_store` | Establishment primarily selling ingredients or ready-made food items. | `types` includes "store" or "deli" alongside restaurant | `types` includes "grocery_or_supermarket" or "store"; reviews focus on shopping, products |
| `brunch` | Establishment where brunch is a speciality. | `serves_brunch: true` and one review mentions brunch, but the restaurant is primarily known for dinner | Multiple reviews highlight brunch specifically; "bottomless", "eggs benedict", "weekend brunch" |
| `outdoor_dining` | Notable outdoor seating that is a draw in itself. | `outdoor_seating: true` and one review mentions outdoor space | `outdoor_seating: true`; multiple reviews mention "terrace", "rooftop", "garden", "al fresco" |
| `wavy` | A restaurant that feels culturally current, novel, or exciting. | cuisine type is slightly uncommon but execution is conventional; one or two reviews mention "interesting" | cuisine or concept feels genuinely novel; reviews use "unique", "you have to try this", "never seen anything like it" |
| `bossman` | A no-frills, purely functional eatery where the food is commoditized and interchangeable. | One or two signals of generic nature but has some distinguishing element | Completely generic; cuisine is mass-market commoditized; no uniqueness language; price_bucket is "budget" |

# Scoring Guidelines

Assign each tag to exactly one of these five categories. Do not use any scores other than 60, 70, 80, 90, or 100.

- **60 — Supported but peripheral**: Valid based on evidence, but describes a minor or incidental aspect.
- **70 — Solid secondary trait**: Clear evidence from at least two sources. Real and noticeable but not what the restaurant is known for.
- **80 — Strong fit**: Multiple pieces of evidence across different fields consistently support this tag.
- **90 — Core identity**: Central to what the restaurant is. Appears consistently across text fields and supported by boolean attributes.
- **100 — Defining characteristic**: Essentially synonymous with the restaurant's identity.

# Scoring Principles

- **Breadth matters**: A tag supported by a boolean, review text, and editorial summary scores higher than one supported by a single field.
- **Centrality matters most**: A restaurant where "romantic" is the first thing every reviewer mentions scores higher than one where it's a passing comment.
- **Contradictions reduce scores**: If signals work against the tag, drop the score by at least one bucket.
- **Don't inflate**: Most relevant tags should score 70-80. Scores of 90+ should be reserved for tags that genuinely define the restaurant.

# Output Format

Return ONLY a valid JSON object with scores for each tag:
```json
{
  "romantic": 80,
  "pub": 90,
  "elegant": 70
}
```

Only include tags that were in the input `relevant_tags` list. Do not add additional tags.
Do not include markdown fences, explanation text, or anything outside the JSON object."""


INVALID_TAG_SCORER = r"""# Role and Objective

You are a scoring model for restaurant vibe tags. You will receive:
1. Complete restaurant data in JSON format
2. A list of tags that have been identified as **irrelevant** with reasons for exclusion

Your task is to assign a numerical score (10-50) to each irrelevant tag.

# Scoring Guidelines

Assign each tag to exactly one of these five categories. Do not use any scores other than 10, 20, 30, 40, or 50.

- **10 — Active contradiction**: The restaurant data directly opposes this tag. A boolean field is explicitly false, or text evidence clearly rules it out.
- **20 — No evidence**: Nothing in the data supports this tag, but nothing actively contradicts it either.
- **30 — Weak signal**: One minor or indirect piece of evidence exists, but it clearly falls short of the tag's definition.
- **40 — Borderline**: Multiple weak signals or one moderate signal exists. The tag plausibly applies but there is not a large amount of it.
- **50 — Near-relevant**: Solid supporting evidence exists with no meaningful contradiction. The tag narrowly missed being classified as relevant.

# Decision Process

1. Read the exclusion reason from pass 1 and the location data.
2. Check for active contradictions (boolean fields explicitly false, text evidence ruling it out) -> 10
3. Check for silence (no signals at all) -> 20
4. Check for weak hints (one minor indirect piece) -> 30
5. Check for borderline (multiple weak signals or one moderate signal) -> 40
6. Check for near-relevant (solid evidence but secondary) -> 50

When the exclusion reason from pass 1 conflicts with what you observe in the data, trust the data over the reason.

# Output Format

Return ONLY a valid JSON object with scores for each tag:
```json
{
  "sports_bar": 10,
  "live_music": 20,
  "food_truck": 20
}
```

Only include tags that were in the input `irrelevant_tags` list. Do not add additional tags.
Do not include markdown fences, explanation text, or anything outside the JSON object."""


# ── LLM Helper ───────────────────────────────────────────────────────────────

async def _call_llm(client: AsyncOpenAI, prompt: str, data: dict) -> dict:
    """Call xAI Grok with a prompt and structured data, return parsed JSON."""
    full_prompt = f"{prompt}\n\n# Input Data\n\n```json\n{json.dumps(data, indent=2, default=str)}\n```"

    response = await client.chat.completions.create(
        model=MODEL,
        max_tokens=2000,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "You are a restaurant analysis assistant. Always respond with valid JSON."},
            {"role": "user", "content": full_prompt},
        ],
    )

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Failed to parse vibe tag LLM response: %s", raw[:200])
        raise


# ── 3-Stage Analysis Pipeline ────────────────────────────────────────────────

async def _analyze_restaurant_vibes(
    restaurant: dict,
    client: AsyncOpenAI,
) -> Dict[str, float]:
    """
    Complete 3-stage analysis pipeline for a single restaurant.

    Returns dict mapping tag names to scores (0-100).
    """
    # Stage 1: Identify relevant and irrelevant tags
    stage1_result = await _call_llm(client, VALID_TAG_PROMPT, restaurant)

    relevant_tags = stage1_result.get("relevant_tags", [])
    irrelevant_tags = stage1_result.get("irrelevant_tags", [])

    logger.info(
        "Vibe stage 1: %d relevant, %d irrelevant tags",
        len(relevant_tags), len(irrelevant_tags),
    )

    # Stage 2: Score relevant tags (60-100)
    stage2_input = {
        "restaurant_data": restaurant,
        "relevant_tags": relevant_tags,
    }
    relevant_scores = await _call_llm(client, VALID_TAG_SCORER, stage2_input)

    # Stage 3: Score irrelevant tags (10-50)
    stage3_input = {
        "restaurant_data": restaurant,
        "irrelevant_tags": irrelevant_tags,
    }
    irrelevant_scores = await _call_llm(client, INVALID_TAG_SCORER, stage3_input)

    # Combine scores, fill missing with 0
    combined_scores = {**relevant_scores, **irrelevant_scores}
    for tag in VIBE_TAGS_ORDERED:
        if tag not in combined_scores:
            combined_scores[tag] = 0.0

    return combined_scores


def _scores_to_vibe_vector(scores: Dict[str, float]) -> List[float]:
    """Convert score dict to ordered vibe_vector array."""
    return [float(scores.get(tag, 0.0)) for tag in VIBE_TAGS_ORDERED]


# ── Top-Level Public Function ────────────────────────────────────────────────

async def generate_vibe_tags_for_location(
    location_id: int,
    num_runs: int = 1,
) -> Optional[List[float]]:
    """
    Generate vibe tags for a location and update its vibe_vector in Supabase.

    Fetches the full location record (including any menu analysis data),
    runs the 3-stage LLM pipeline, and writes the resulting vibe_vector.

    Args:
        location_id: The Pinit location ID in Supabase.
        num_runs: Number of analysis runs to average (use >1 for higher
            accuracy on locations with rich data like generated_summary).

    Returns:
        The computed vibe_vector on success, None on fatal error.
    """
    if not XAI_API_KEY:
        logger.error("XAI_API_KEY not configured, skipping vibe tagging for location %s", location_id)
        return None

    supabase = get_supabase_service()

    # Fetch full restaurant record (with any recently-updated menu fields)
    restaurant = supabase.get_location(location_id)
    if not restaurant:
        logger.error("Location %s not found in Supabase, cannot generate vibe tags", location_id)
        return None

    name = restaurant.get("name", "Unknown")
    logger.info("Starting vibe tag generation for location %s (%s), num_runs=%d", location_id, name, num_runs)

    client = AsyncOpenAI(api_key=XAI_API_KEY, base_url=XAI_BASE_URL)

    try:
        if num_runs > 1:
            # Multiple runs — compute mean scores
            import statistics
            all_scores: List[Dict[str, float]] = []

            for run_num in range(num_runs):
                try:
                    scores = await _analyze_restaurant_vibes(restaurant, client)
                    all_scores.append(scores)
                except Exception as e:
                    logger.warning("Vibe run %d/%d failed for location %s: %s", run_num + 1, num_runs, location_id, e)

            if not all_scores:
                logger.error("All vibe tag runs failed for location %s", location_id)
                return None

            mean_scores: Dict[str, float] = {}
            for tag in VIBE_TAGS_ORDERED:
                tag_values = [s.get(tag, 0.0) for s in all_scores]
                mean_scores[tag] = statistics.mean(tag_values)

            vibe_vector = _scores_to_vibe_vector(mean_scores)
            logger.info("Vibe tagging complete for location %s — averaged %d/%d runs", location_id, len(all_scores), num_runs)
        else:
            # Single run
            scores = await _analyze_restaurant_vibes(restaurant, client)
            vibe_vector = _scores_to_vibe_vector(scores)

    except Exception as e:
        logger.error("Vibe tag generation failed for location %s: %s", location_id, e, exc_info=True)
        return None

    # Update Supabase
    try:
        supabase.update_location(location_id, vibe_vector=vibe_vector, updated_vibe=True)
        logger.info("Updated vibe_vector for location %s: %s", location_id, vibe_vector)
    except Exception as e:
        logger.error("Failed to update vibe_vector for location %s: %s", location_id, e, exc_info=True)
        return None

    return vibe_vector
