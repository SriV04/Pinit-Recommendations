from __future__ import annotations

import json
import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from config import PipelinePaths, ReviewTagConfig
from tag_taxonomy import tag_dataframe


CATEGORY_TYPES = {
    "restaurant": "restaurant",
    "meal_delivery": "takeaway",
    "meal_takeaway": "takeaway",
    "cafe": "cafe",
    "bar": "bar",
}

REVIEW_TAG_KEYWORDS: Dict[str, Dict[str, Sequence[str]]] = {
    "cozy": {"keywords": ["cozy", "cosy", "snug", "warm lighting"]},
    "romantic": {"keywords": ["romantic", "date night", "special date"]},
    "lively": {"keywords": ["lively", "buzzy", "energetic", "party"]},
    "quiet": {"keywords": ["quiet", "peaceful", "calm", "relaxed"]},
    "trendy": {"keywords": ["trendy", "instagrammable", "aesthetic"]},
    "casual": {"keywords": ["casual", "laid back", "chill vibes"]},
    "formal": {"keywords": ["formal", "fine dining", "tasting menu"]},
    "family_friendly": {"keywords": ["family", "kids", "child friendly", "pram"]},
    "date_night": {"keywords": ["date night", "romantic", "anniversary"]},
    "brunch": {"keywords": ["brunch", "poached eggs", "avocado toast"]},
    "quick_bite": {"keywords": ["quick bite", "fast service", "grab and go"]},
    "group_hang": {"keywords": ["group of friends", "hen party", "stag do"]},
    "business_meeting": {"keywords": ["business meeting", "client lunch", "power lunch"]},
    "solo_friendly": {"keywords": ["solo", "ate alone", "counter seating"]},
    "cocktails": {"keywords": ["cocktails", "mixology", "negroni", "margarita"]},
    "wine_bar": {"keywords": ["wine list", "sommelier", "wine flight"]},
    "craft_beer": {"keywords": ["craft beer", "tap list", "ipa", "lager"]},
    "vegetarian_friendly": {"keywords": ["vegetarian options", "vegetarian friendly"]},
    "vegan_friendly": {"keywords": ["vegan options", "plant based", "plant-based"]},
    "halal_friendly": {"keywords": ["halal", "halal friendly"]},
    "gluten_free_options": {"keywords": ["gluten free", "gluten-free", "celiac"]},
}


def _min_max_scale(values: pd.Series) -> pd.Series:
    v_min = values.min()
    v_max = values.max()
    if math.isclose(v_min, v_max):
        return pd.Series(np.zeros(len(values)), index=values.index)
    return (values - v_min) / (v_max - v_min)


def _safe_json_loads(raw: str | float | int | None) -> List[Dict]:
    if raw is None:
        return []
    if isinstance(raw, float) and math.isnan(raw):
        return []
    if isinstance(raw, str) and raw.strip() == "":
        return []
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return []


def _hhmm_to_minutes(value: str | None) -> Optional[int]:
    if not value or not isinstance(value, str):
        return None
    try:
        h = int(value[:2])
        m = int(value[2:])
    except ValueError:
        return None
    return h * 60 + m


def _schedule_flags(periods_raw: str | None) -> Dict[str, bool]:
    periods = _safe_json_loads(periods_raw)
    if not periods:
        return {"open_late": False, "open_early": False, "sunday_open": False}
    open_late = False
    open_early = False
    sunday_open = False
    for p in periods:
        open_info = p.get("open", {})
        close_info = p.get("close", {})
        open_time = _hhmm_to_minutes(open_info.get("time"))
        close_time = _hhmm_to_minutes(close_info.get("time"))
        open_day = open_info.get("day")
        close_day = close_info.get("day")
        if open_time is not None and open_time <= 8 * 60:
            open_early = True
        if close_time is not None:
            if close_day is not None and open_day is not None and close_day != open_day:
                open_late = True
            elif close_time >= 23 * 60:
                open_late = True
        if open_day == 0 or close_day == 0:
            sunday_open = True
    return {"open_late": open_late, "open_early": open_early, "sunday_open": sunday_open}


def _price_bucket(price_level: float | int | None) -> str:
    if price_level is None or math.isnan(price_level):
        return "unknown"
    if price_level <= 1:
        return "value"
    if price_level == 2:
        return "mid"
    if price_level >= 3:
        return "premium"
    return "unknown"


def load_locations(paths: PipelinePaths) -> pd.DataFrame:
    details = pd.read_csv(paths.details_csv())
    base_df = pd.read_csv(paths.base_csv()) if paths.base_csv().exists() else None

    df = details.copy()
    df["location_id"] = np.arange(len(df)) + 1
    df["google_place_id"] = df["place_id"]
    df["name"] = df["name"].fillna("")
    df["vicinity"] = df["vicinity"].fillna("")
    df["rating"] = pd.to_numeric(df.get("rating"), errors="coerce")
    df["user_ratings_total"] = pd.to_numeric(df.get("user_ratings_total"), errors="coerce").fillna(0)
    df["price_level"] = pd.to_numeric(df.get("price_level"), errors="coerce")
    df["lat"] = pd.to_numeric(df.get("lat"), errors="coerce")
    df["lon"] = pd.to_numeric(df.get("lon"), errors="coerce")

    cuisine = df.get("cuisine_detected_ext")
    if cuisine is None:
        cuisine = df.get("cuisine_detected")
    df["cuisine_primary"] = (
        cuisine.fillna(df.get("cuisine_detected")).fillna("unknown").astype(str).str.lower()
    )

    if base_df is not None and "grid_id" in base_df.columns:
        df = df.merge(base_df[["place_id", "grid_id"]], on="place_id", how="left")
    else:
        df["grid_id"] = np.nan

    types_series = df.get("types", pd.Series([""] * len(df)))
    df["types_list"] = types_series.fillna("").apply(
        lambda s: [t.strip() for t in str(s).split(",") if t.strip()]
    )

    schedule_flags = df.get("opening_hours_periods", pd.Series([""] * len(df))).apply(
        _schedule_flags
    )
    df["is_open_late"] = schedule_flags.apply(lambda x: x["open_late"])
    df["is_open_early"] = schedule_flags.apply(lambda x: x["open_early"])
    df["is_sunday_open"] = schedule_flags.apply(lambda x: x["sunday_open"])

    df["price_bucket"] = df["price_level"].apply(_price_bucket)
    df["log_reviews"] = np.log1p(df["user_ratings_total"])
    df["popularity_score"] = _min_max_scale(df["log_reviews"].fillna(0))

    by_group = (
        df.groupby(["cuisine_primary", "price_bucket"])["log_reviews"]
        .transform("mean")
        .fillna(df["log_reviews"].mean())
    )
    df["expected_popularity"] = by_group
    df["residual_popularity"] = df["log_reviews"] - df["expected_popularity"]
    df["hidden_gem_score"] = _min_max_scale(df["residual_popularity"].clip(upper=0).abs())
    df["quality_score"] = _min_max_scale(df["rating"].fillna(df["rating"].mean()))

    return df


def load_reviews(paths: PipelinePaths, place_to_location: Dict[str, int]) -> pd.DataFrame:
    reviews_path = paths.reviews_csv()
    if not reviews_path.exists():
        return pd.DataFrame(columns=["location_id", "language", "author_name", "text"])
    reviews = pd.read_csv(reviews_path)
    reviews["location_id"] = reviews["place_id"].map(place_to_location)
    reviews = reviews.dropna(subset=["location_id"])
    reviews["text"] = reviews["text"].fillna("")
    reviews["language"] = reviews.get("language", "").fillna("")
    reviews["author_name"] = reviews.get("author_name", "").fillna("anon")
    return reviews


def _add_tag_record(records: List[Dict], location_id: int, tag_text: str, score: float, source: str, metadata: Dict):
    records.append(
        {
            "location_id": location_id,
            "tag_text": tag_text,
            "score": float(score),
            "source": source,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }
    )


def _deterministic_tags(df: pd.DataFrame) -> List[Dict]:
    records: List[Dict] = []
    for row in df.itertuples():
        if row.cuisine_primary and row.cuisine_primary != "unknown":
            _add_tag_record(
                records,
                row.location_id,
                row.cuisine_primary,
                92,
                "cuisine_detected",
                {"field": "cuisine_primary"},
            )

        for t in row.types_list:
            if t in CATEGORY_TYPES:
                tag_text = CATEGORY_TYPES[t]
                _add_tag_record(
                    records,
                    row.location_id,
                    tag_text,
                    75,
                    "google_types",
                    {"type": t},
                )

        if row.price_bucket == "value":
            _add_tag_record(
                records,
                row.location_id,
                "great_value",
                80,
                "price_level",
                {"price_level": row.price_level},
            )
        elif row.price_bucket == "premium":
            _add_tag_record(
                records,
                row.location_id,
                "pricey",
                80,
                "price_level",
                {"price_level": row.price_level},
            )

        if row.is_open_late:
            _add_tag_record(
                records,
                row.location_id,
                "open_late",
                70,
                "opening_hours",
                {},
            )
        if row.is_open_early:
            _add_tag_record(
                records,
                row.location_id,
                "open_early",
                70,
                "opening_hours",
                {},
            )
        if row.is_sunday_open:
            _add_tag_record(
                records,
                row.location_id,
                "sunday_open",
                65,
                "opening_hours",
                {},
            )
    return records


def _review_tag_records(
    reviews: pd.DataFrame, config: ReviewTagConfig
) -> List[Dict]:
    if reviews.empty:
        return []
    df = reviews.copy()
    if config.english_only:
        df = df[df["language"].str.startswith("en")]
    df["text_norm"] = df["text"].str.lower()
    grouped: Dict[Tuple[int, str], Dict[str, set]] = {}
    for row in df.itertuples():
        text = row.text_norm
        found_tags = []
        for tag_text, meta in REVIEW_TAG_KEYWORDS.items():
            for kw in meta["keywords"]:
                if kw in text:
                    found_tags.append(tag_text)
                    break
        if not found_tags:
            continue
        for tag_text in found_tags:
            key = (int(row.location_id), tag_text)
            entry = grouped.setdefault(key, {"authors": set(), "mentions": 0})
            entry["mentions"] += 1
            entry["authors"].add(row.author_name or "anon")

    records: List[Dict] = []
    for (location_id, tag_text), info in grouped.items():
        mentions = info["mentions"]
        unique_authors = len(info["authors"])
        if unique_authors < config.min_unique_authors and mentions < config.min_mentions:
            continue
        score = config.score_floor + 15 * math.log1p(unique_authors) + 10 * math.log1p(mentions)
        score = min(config.score_cap, score)
        _add_tag_record(
            records,
            location_id,
            tag_text,
            score,
            "reviews",
            {"mentions": mentions, "unique_authors": unique_authors},
        )
    return records


def build_location_tags(
    locations: pd.DataFrame, reviews: pd.DataFrame, config: ReviewTagConfig
) -> pd.DataFrame:
    deterministic = _deterministic_tags(locations)
    review_based = _review_tag_records(reviews, config)
    records = deterministic + review_based
    tags_df = tag_dataframe()
    location_tags = pd.DataFrame(records)
    if location_tags.empty:
        return pd.DataFrame(
            columns=["location_id", "tag_id", "tag_text", "score", "source", "metadata"]
        )
    location_tags = location_tags.merge(tags_df[["tag_id", "text"]], left_on="tag_text", right_on="text", how="inner")
    location_tags = location_tags.drop(columns=["text"])
    return location_tags


