from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List

import pandas as pd


@dataclass(frozen=True)
class TagDefinition:
    text: str
    tag_type: str
    prompt_description: str
    color: str


TAG_COLOR_BY_TYPE: Dict[str, str] = {
    "CUISINE": "#F94144",
    "DIETARY": "#90BE6D",
    "VIBE": "#577590",
    "OCCASION": "#F9C74F",
    "DRINKS": "#277DA1",
    "SCHEDULE": "#F3722C",
    "VALUE": "#8338EC",
    "CATEGORY": "#4D908E",
}


def _make_def(text: str, tag_type: str, prompt: str, color: str | None = None) -> TagDefinition:
    return TagDefinition(
        text=text,
        tag_type=tag_type,
        prompt_description=prompt,
        color=color or TAG_COLOR_BY_TYPE.get(tag_type, "#999999"),
    )


def _cuisine_tags() -> Iterable[TagDefinition]:
    cuisine_prompts = {
        "italian": "Classic Italian spots for pasta, pizza and Aperol.",
        "indian": "Curries, tandoor smoke and bold spice.",
        "japanese": "Sushi counters, ramen dens and izakayas.",
        "korean": "BBQ grills, bibimbap and kimchi cravings.",
        "thai": "Sweet-savoury Thai plates and night-market energy.",
        "chinese": "Regional Chinese kitchens from dim sum to Sichuan heat.",
        "vietnamese": "Pho, bánh mì and herb-loaded broths.",
        "mexican": "Taquerias, mezcal bars and modern cantinas.",
        "mediterranean": "Sun-soaked mezze and grilled plates.",
        "british": "Modern British kitchens and proper roasts.",
        "pub": "True pub fare with pints and Sunday sessions.",
        "bakery": "Bakeries, patisseries and pastry labs.",
        "cafe": "Coffee-forward cafes and brunch joints.",
        "seafood": "Raw bars, shellfish shacks and seafood grills.",
        "steakhouse": "Steakhouses and grill houses.",
        "vegan_vegetarian": "Veggie-first kitchens and plant-based menus.",
    }
    for text, prompt in cuisine_prompts.items():
        yield _make_def(text, "CUISINE", prompt)


def _dietary_tags() -> Iterable[TagDefinition]:
    dietary_prompts = {
        "vegetarian_friendly": "Menus with strong vegetarian sections.",
        "vegan_friendly": "Vegan-friendly options beyond token salads.",
        "halal_friendly": "Halal-friendly kitchens.",
        "gluten_free_options": "Staff that knows their gluten-free swaps.",
    }
    for text, prompt in dietary_prompts.items():
        yield _make_def(text, "DIETARY", prompt)


def _vibe_tags() -> Iterable[TagDefinition]:
    vibe_prompts = {
        "cozy": "Candle-lit, intimate and low-key rooms.",
        "romantic": "Date-night lighting with wow plates.",
        "lively": "Music up, energy high.",
        "quiet": "Calm corners perfect for catching up.",
        "trendy": "Design-led, camera-ready settings.",
        "casual": "Laid-back hangouts and counter service.",
        "formal": "White tablecloths or tasting menus.",
        "family_friendly": "Space for prams and picky eaters.",
    }
    for text, prompt in vibe_prompts.items():
        yield _make_def(text, "VIBE", prompt)


def _occasion_tags() -> Iterable[TagDefinition]:
    occasion_prompts = {
        "date_night": "Romantic nights out.",
        "brunch": "Sunny brunch plates and coffee refills.",
        "quick_bite": "In-and-out meals under an hour.",
        "group_hang": "Space for friend groups and celebrations.",
        "business_meeting": "Laptop-friendly or client-ready rooms.",
        "solo_friendly": "Bar seats or counter dining for one.",
    }
    for text, prompt in occasion_prompts.items():
        yield _make_def(text, "OCCASION", prompt)


def _drinks_tags() -> Iterable[TagDefinition]:
    drinks_prompts = {
        "cocktails": "Serious cocktails or signature serves.",
        "wine_bar": "Deep wine lists and low-slung lighting.",
        "craft_beer": "Rotating taps and local brews.",
    }
    for text, prompt in drinks_prompts.items():
        yield _make_def(text, "DRINKS", prompt)


def _schedule_tags() -> Iterable[TagDefinition]:
    schedule_prompts = {
        "open_late": "Kitchen keeps going past 11pm.",
        "open_early": "Serving before 8am.",
        "sunday_open": "Open on Sundays.",
    }
    for text, prompt in schedule_prompts.items():
        yield _make_def(text, "SCHEDULE", prompt)


def _value_tags() -> Iterable[TagDefinition]:
    return (
        _make_def("great_value", "VALUE", "Serious bang for buck."),
        _make_def("pricey", "VALUE", "Splurge-worthy but spendy."),
        _make_def("hidden_gem", "VALUE", "Under-the-radar hits that outperform their hype."),
    )


def _category_tags() -> Iterable[TagDefinition]:
    prompts = {
        "restaurant": "Full-service restaurants.",
        "cafe": "Coffee + daytime cafes.",
        "bar": "Cocktail and wine bars.",
        "takeaway": "Grab-and-go friendly.",
    }
    for text, prompt in prompts.items():
        yield _make_def(text, "CATEGORY", prompt)


def iter_tag_definitions() -> List[TagDefinition]:
    defs: List[TagDefinition] = []
    for builder in (
        _cuisine_tags,
        _dietary_tags,
        _vibe_tags,
        _occasion_tags,
        _drinks_tags,
        _schedule_tags,
        _value_tags,
        _category_tags,
    ):
        defs.extend(builder())
    return defs


def tag_dataframe() -> pd.DataFrame:
    defs = iter_tag_definitions()
    rows = []
    for tag_id, definition in enumerate(defs, start=1):
        rows.append(
            {
                "tag_id": tag_id,
                "text": definition.text,
                "tag_type": definition.tag_type,
                "prompt_description": definition.prompt_description,
                "color": definition.color,
            }
        )
    return pd.DataFrame(rows)


def tag_lookup() -> Dict[str, TagDefinition]:
    return {d.text: d for d in iter_tag_definitions()}
