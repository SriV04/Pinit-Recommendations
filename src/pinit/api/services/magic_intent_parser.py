from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, List

from pinit.api.schemas_magic import MagicIntent


_CUISINE_ALIASES = {
    "thai": "thai",
    "sushi": "sushi",
    "japanese": "japanese",
    "italian": "italian",
    "pizza": "pizza",
    "indian": "indian",
    "mexican": "mexican",
    "chinese": "chinese",
    "korean": "korean",
    "vietnamese": "vietnamese",
    "french": "french",
    "greek": "greek",
    "mediterranean": "mediterranean",
    "lebanese": "lebanese",
    "turkish": "turkish",
    "vegan": "vegan",
    "vegetarian": "vegetarian",
}

_BUDGET_TERMS = {
    "cheap": "cheap",
    "budget": "cheap",
    "affordable": "cheap",
    "inexpensive": "cheap",
    "moderate": "moderate",
    "midrange": "moderate",
    "fancy": "expensive",
    "expensive": "expensive",
    "splurge": "expensive",
}

_DIETARY_TERMS = {
    "vegan": "vegan",
    "vegetarian": "vegetarian",
    "gluten free": "gluten_free",
    "gluten-free": "gluten_free",
    "halal": "halal",
    "kosher": "kosher",
    "dairy free": "dairy_free",
    "dairy-free": "dairy_free",
}

_VIBE_PHRASES = {
    "bougie": ["upscale", "stylish"],
    "formal": ["formal", "fine_dining"],
    "fine dining": ["formal", "fine_dining"],
    "vibey": ["trendy", "casual"],
    "trendy": ["trendy"],
    "cute": ["cute"],
    "cosy": ["cosy"],
    "cozy": ["cosy"],
    "romantic": ["romantic"],
    "date night": ["romantic", "cosy"],
    "hidden gem": ["hidden_gem"],
    "casual": ["casual"],
    "lively": ["lively"],
    "quiet": ["quiet"],
    "outdoor": ["outdoor_seating"],
    "terrace": ["outdoor_seating"],
    "rooftop": ["rooftop"],
}

_GOOGLE_QUERY_MODIFIERS = {
    "formal": "formal",
    "fine dining": "fine dining",
}

_OCCASION_PHRASES = {
    "date night": "date_night",
    "date": "date_night",
    "group": "group",
    "groups": "group",
    "birthday": "birthday",
    "work": "work",
    "brunch": "brunch",
}

_TIME_PHRASES = {
    "open now": "open_now",
    "tonight": "tonight",
    "late night": "late_night",
    "now": "now",
    "lunch": "lunch",
    "dinner": "dinner",
    "breakfast": "breakfast",
    "brunch": "brunch",
}

_NEGATIVE_PHRASES = {
    "not touristy": "touristy",
    "no tourists": "touristy",
    "avoid touristy": "touristy",
    "not expensive": "expensive",
    "not fancy": "fancy",
}

_STOPWORDS = {
    "a",
    "an",
    "and",
    "around",
    "at",
    "by",
    "for",
    "in",
    "me",
    "near",
    "nearby",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}

_LOCATION_BREAK_WORDS = {
    "for",
    "with",
    "tonight",
    "today",
    "tomorrow",
    "now",
    "open",
    "cheap",
    "budget",
    "affordable",
    "vibey",
    "bougie",
    "cute",
    "cosy",
    "cozy",
    "date",
    "group",
    "groups",
}


def normalise_prompt(prompt: str) -> str:
    text = prompt.strip().lower()
    text = re.sub(r"[^a-z0-9&+\-\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def hash_intent(value: Any) -> str:
    if isinstance(value, MagicIntent):
        payload = value.model_dump(mode="json")
    else:
        payload = value
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def _unique(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _extract_cuisines(text: str) -> List[str]:
    matches = [
        canonical
        for term, canonical in _CUISINE_ALIASES.items()
        if _contains_phrase(text, term)
    ]
    return _unique(matches)


def _extract_vibes(text: str) -> List[str]:
    vibes: List[str] = []
    for phrase, tags in _VIBE_PHRASES.items():
        if _contains_phrase(text, phrase):
            vibes.extend(tags)
    if _contains_phrase(text, "group") or _contains_phrase(text, "groups"):
        vibes.append("good_for_groups")
    return _unique(vibes)


def _extract_first(text: str, phrases: dict[str, str]) -> str | None:
    for phrase, value in phrases.items():
        if _contains_phrase(text, phrase):
            return value
    return None


def _extract_all(text: str, phrases: dict[str, str]) -> List[str]:
    return _unique(
        value for phrase, value in phrases.items() if _contains_phrase(text, phrase)
    )


def _extract_location_name(text: str) -> str | None:
    match = re.search(r"\b(?:in|near|around)\s+(.+)$", text)
    if not match:
        return None

    words = match.group(1).strip().split()
    location_words: List[str] = []
    for word in words:
        if word in _LOCATION_BREAK_WORDS:
            break
        location_words.append(word)

    location = " ".join(location_words).strip()
    if not location or location in {"me", "nearby"}:
        return None
    return location


def _included_types(text: str, cuisines: List[str], occasion: str | None) -> List[str]:
    if _contains_phrase(text, "coffee") or _contains_phrase(text, "cafe"):
        return ["cafe"]
    if _contains_phrase(text, "bar") or _contains_phrase(text, "cocktail"):
        return ["bar"]
    if occasion == "brunch":
        return ["restaurant"]
    if cuisines:
        return ["restaurant"]
    return ["restaurant"]


def _with_location(query: str, location_name: str | None) -> str:
    if not location_name:
        return query
    return f"{query} in {location_name}"


def _extract_google_query_modifiers(text: str) -> List[str]:
    return _unique(
        modifier
        for phrase, modifier in _GOOGLE_QUERY_MODIFIERS.items()
        if _contains_phrase(text, phrase)
    )


def _apply_query_modifiers(
    query: str,
    query_modifiers: List[str],
    time_context: str | None,
) -> str:
    prefixes = list(query_modifiers)
    if time_context in {"breakfast", "brunch", "lunch", "dinner"}:
        prefixes.append(time_context)
    prefixes = [
        prefix
        for prefix in _unique(prefixes)
        if not query.startswith(f"{prefix} ")
    ]
    if not prefixes:
        return query
    return f"{' '.join(prefixes)} {query}"


def _cleaned_query(
    text: str,
    cuisines: List[str],
    included_types: List[str],
    location_name: str | None,
    query_modifiers: List[str],
    time_context: str | None,
) -> str:
    if _contains_phrase(text, "coffee"):
        base_query = "coffee shop"
    elif _contains_phrase(text, "cafe"):
        base_query = "cafe"
    elif _contains_phrase(text, "brunch"):
        base_query = "brunch restaurant"
    elif cuisines:
        base_query = f"{cuisines[0]} restaurant"
    elif "bar" in included_types:
        base_query = "bar"
    else:
        base_query = "restaurant"

    query = _apply_query_modifiers(base_query, query_modifiers, time_context)
    return _with_location(query, location_name)


def build_google_queries(intent: MagicIntent) -> List[str]:
    queries = [intent.cleaned_query]
    cuisine = intent.cuisines[0] if intent.cuisines else None

    if cuisine:
        location_suffix = f" in {intent.location_name}" if intent.location_name else ""
        if "casual" in intent.vibe_tags:
            queries.append(f"casual {cuisine} restaurant{location_suffix}")
        queries.append(f"{cuisine} food{location_suffix}")
    elif intent.occasion == "brunch":
        queries.append(_with_location("brunch near me", intent.location_name))
    elif intent.budget == "cheap":
        queries.append(_with_location("cheap eats", intent.location_name))

    return _unique(q for q in queries if q)[:3]


def parse_magic_intent(prompt: str) -> MagicIntent:
    normalised = normalise_prompt(prompt)
    cuisines = _extract_cuisines(normalised)
    vibes = _extract_vibes(normalised)
    occasion = _extract_first(normalised, _OCCASION_PHRASES)
    budget = _extract_first(normalised, _BUDGET_TERMS)
    dietary = _extract_all(normalised, _DIETARY_TERMS)
    time_context = _extract_first(normalised, _TIME_PHRASES)
    negative_terms = _extract_all(normalised, _NEGATIVE_PHRASES)
    included_types = _included_types(normalised, cuisines, occasion)
    location_name = _extract_location_name(normalised)
    query_modifiers = _extract_google_query_modifiers(normalised)
    cleaned_query = _cleaned_query(
        normalised,
        cuisines,
        included_types,
        location_name,
        query_modifiers,
        time_context,
    )

    tokens = [
        token
        for token in normalised.split()
        if token not in _STOPWORDS and token not in negative_terms
    ]

    strict_constraints: dict[str, Any] = {}
    if time_context == "open_now":
        strict_constraints["open_now"] = True
    if "vegan" in dietary:
        strict_constraints["serves_vegetarian_food"] = True

    soft_preferences = {
        key: value
        for key, value in {
            "budget": budget,
            "occasion": occasion,
            "vibe_tags": vibes,
            "time_context": time_context,
        }.items()
        if value
    }

    intent = MagicIntent(
        raw_prompt=prompt,
        cleaned_query=cleaned_query,
        cuisines=cuisines,
        vibe_tags=vibes,
        occasion=occasion,
        budget=budget,
        dietary=dietary,
        time_context=time_context,
        location_name=location_name,
        positive_terms=_unique(tokens),
        negative_terms=negative_terms,
        strict_constraints=strict_constraints,
        soft_preferences=soft_preferences,
        included_types=included_types,
    )
    intent.google_queries = build_google_queries(intent)
    return intent
