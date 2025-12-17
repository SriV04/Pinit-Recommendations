from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

import numpy as np
import pandas as pd

from config import (
    DEFAULT_ACTION_WEIGHTS,
    PipelinePaths,
    RECENCY_HALFLIFE_DAYS,
)


SYNTHETIC_PROFILES = [
    {"user_id": "demo_date_night", "tags": ["date_night", "italian", "wine_bar"]},
    {"user_id": "demo_vegan", "tags": ["vegan_vegetarian", "vegan_friendly", "cafe"]},
    {"user_id": "demo_group_hang", "tags": ["group_hang", "mexican", "cocktails"]},
]


def load_user_actions(paths: PipelinePaths) -> pd.DataFrame:
    candidate_paths = [
        paths.user_actions_csv,
        paths.data_dir / "user_location_actions.csv",
    ]
    for candidate in candidate_paths:
        if candidate and candidate.exists():
            df = pd.read_csv(candidate)
            return df
    return pd.DataFrame(columns=["user_id", "place_id", "action", "created_at"])


def synthesize_user_actions(
    locations: pd.DataFrame, location_tags: pd.DataFrame, profiles: Iterable[Dict] | None = None
) -> pd.DataFrame:
    profiles = list(profiles) if profiles else SYNTHETIC_PROFILES
    rng = np.random.default_rng(42)
    place_lookup = locations.set_index("location_id")["google_place_id"].to_dict()
    tag_scores = (
        location_tags.groupby(["tag_text", "location_id"])["score"]
        .mean()
        .reset_index()
    )
    rows = []
    for profile in profiles:
        tags = profile.get("tags", [])
        subset = tag_scores[tag_scores["tag_text"].isin(tags)]
        if subset.empty:
            continue
        subset = (
            subset.sort_values(by="score", ascending=False)
            .groupby("location_id")
            .first()
            .reset_index()
        )
        chosen = subset.head(12)
        for _, record in chosen.iterrows():
            loc_id = int(record["location_id"])
            place_id = place_lookup.get(loc_id)
            if not place_id:
                continue
            action = rng.choice(["save", "like", "detail_view"], p=[0.4, 0.4, 0.2])
            days_ago = float(rng.integers(0, 90))
            timestamp = pd.Timestamp.utcnow() - pd.Timedelta(days=days_ago)
            rows.append(
                {
                    "user_id": profile["user_id"],
                    "place_id": place_id,
                    "action": action,
                    "created_at": timestamp.isoformat(),
                }
            )
    return pd.DataFrame(rows)


def ensure_user_actions(
    paths: PipelinePaths,
    locations: pd.DataFrame,
    location_tags: pd.DataFrame,
    allow_synthetic: bool = False,
) -> Tuple[pd.DataFrame, bool]:
    user_actions = load_user_actions(paths)
    if user_actions.empty and allow_synthetic:
        return synthesize_user_actions(locations, location_tags), True
    return user_actions, False


def _apply_action_weights(actions: pd.DataFrame) -> pd.DataFrame:
    df = actions.copy()
    df["action"] = df["action"].astype(str).str.lower()
    df["weight"] = df["action"].map(DEFAULT_ACTION_WEIGHTS).fillna(0.0)
    if "created_at" in df.columns:
        timestamps = pd.to_datetime(df["created_at"], errors="coerce", utc=True)
        age_days = (pd.Timestamp.utcnow().tz_convert("UTC") - timestamps).dt.total_seconds() / 86400.0
        age_days = age_days.fillna(0.0).clip(lower=0.0)
        decay = np.exp(-age_days / RECENCY_HALFLIFE_DAYS)
    else:
        decay = 1.0
    df["weight"] = df["weight"].astype(float) * decay
    df = df[df["weight"] != 0]
    return df


def build_user_tag_affinities(
    user_actions: pd.DataFrame,
    location_tags: pd.DataFrame,
    locations: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if user_actions.empty or location_tags.empty:
        return (
            pd.DataFrame(columns=["user_id", "tag_id", "tag_text", "score", "metadata"]),
            pd.DataFrame(columns=["user_id", "n_actions"]),
        )

    place_to_location = locations.set_index("google_place_id")["location_id"].to_dict()
    df_actions = user_actions.copy()
    df_actions["location_id"] = df_actions["place_id"].map(place_to_location)
    df_actions = df_actions.dropna(subset=["location_id"])
    df_actions["location_id"] = df_actions["location_id"].astype(int)
    df_actions = _apply_action_weights(df_actions)
    if df_actions.empty:
        return (
            pd.DataFrame(columns=["user_id", "tag_id", "tag_text", "score", "metadata"]),
            pd.DataFrame(columns=["user_id", "n_actions"]),
        )

    merged = df_actions.merge(
        location_tags[["location_id", "tag_id", "tag_text", "score"]],
        on="location_id",
        how="inner",
    )
    if merged.empty:
        return (
            pd.DataFrame(columns=["user_id", "tag_id", "tag_text", "score", "metadata"]),
            pd.DataFrame(columns=["user_id", "n_actions"]),
        )

    merged["contrib"] = merged["weight"] * (merged["score"] / 100.0)
    agg = (
        merged.groupby(["user_id", "tag_id", "tag_text"])["contrib"]
        .sum()
        .reset_index()
    )

    def _normalize(group: pd.DataFrame) -> pd.DataFrame:
        max_val = group["contrib"].max()
        if max_val <= 0:
            group["score"] = 0.0
        else:
            group["score"] = (group["contrib"] / max_val) * 100.0
        return group

    normalized = agg.groupby("user_id", group_keys=False).apply(_normalize)
    normalized = normalized.sort_values(by=["user_id", "score"], ascending=[True, False])
    normalized["metadata"] = normalized["contrib"].apply(
        lambda val: json.dumps({"raw_score": val}, ensure_ascii=False)
    )
    normalized = normalized.drop(columns=["contrib"])
    normalized = normalized.groupby("user_id").head(25).reset_index(drop=True)

    user_history = (
        df_actions.groupby("user_id")
        .size()
        .reset_index(name="n_actions")
        .sort_values(by="n_actions", ascending=False)
    )

    return normalized, user_history
