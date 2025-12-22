from __future__ import annotations

import json
from dataclasses import asdict
from typing import Dict, Iterable, List, Sequence, Set, Tuple

import pandas as pd

from config import PipelineConfig, RecommendationWeights


def _adaptive_weights(base: RecommendationWeights, history_size: int) -> Dict[str, float]:
    weights = asdict(base).copy()
    taste_weight = weights.get("taste", 0.0)
    if history_size < 5:
        cold_ratio = history_size / 5.0
        reduction = taste_weight * (1 - cold_ratio)
        weights["taste"] = taste_weight * cold_ratio
        weights["trend_app"] += reduction * 0.6
        weights["quality"] += reduction * 0.4
    total = sum(weights.values())
    if total == 0:
        return weights
    for key in weights:
        weights[key] /= total
    return weights


def _taste_contributions(user_tags: pd.DataFrame, location_tags: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[Tuple[str, int], List[Dict[str, float]]]]:
    if user_tags.empty or location_tags.empty:
        empty = pd.DataFrame(columns=["user_id", "location_id", "taste_score"])
        return empty, {}
    merged = user_tags.merge(
        location_tags[["location_id", "tag_id", "tag_text", "score"]],
        on="tag_id",
        how="inner",
    )
    merged = merged.rename(
        columns={
            "score_x": "user_tag_score",
            "score_y": "location_tag_score",
            "tag_text_x": "user_tag_text",
            "tag_text_y": "location_tag_text",
        }
    )
    merged["component"] = (merged["user_tag_score"] / 100.0) * (merged["location_tag_score"] / 100.0)
    taste_scores = (
        merged.groupby(["user_id", "location_id"])["component"]
        .sum()
        .reset_index(name="taste_score")
    )
    merged = merged.sort_values(by="component", ascending=False)
    detail_map: Dict[Tuple[str, int], List[Dict[str, float]]] = {}
    for row in merged.itertuples():
        key = (row.user_id, int(row.location_id))
        entry = detail_map.setdefault(key, [])
        if len(entry) < 3:
            entry.append({"tag": row.location_tag_text, "score": round(row.component, 3)})
    return taste_scores, detail_map


def _candidate_sets(
    taste_scores: pd.DataFrame,
    top_trend_ids: Sequence[int],
    top_hidden_ids: Sequence[int],
    all_user_ids: Sequence[str],
) -> Dict[str, Set[int]]:
    candidates: Dict[str, Set[int]] = {}
    for row in taste_scores.itertuples():
        candidates.setdefault(row.user_id, set()).add(int(row.location_id))
    top_trend = set(map(int, top_trend_ids))
    top_hidden = set(map(int, top_hidden_ids))
    for user_id in set(candidates.keys()).union(set(all_user_ids)):
        bucket = candidates.setdefault(user_id, set())
        bucket.update(top_trend)
        bucket.update(top_hidden)
    return candidates


def _user_seen_locations(user_actions: pd.DataFrame, locations: pd.DataFrame) -> Dict[str, Set[int]]:
    if user_actions.empty:
        return {}
    place_to_location = locations.set_index("google_place_id")["location_id"].to_dict()
    actions = user_actions.copy()
    actions["location_id"] = actions["place_id"].map(place_to_location)
    actions = actions.dropna(subset=["location_id"])
    grouped: Dict[str, Set[int]] = {}
    for row in actions.itertuples():
        grouped.setdefault(row.user_id, set()).add(int(row.location_id))
    return grouped


def build_recommendations(
    locations: pd.DataFrame,
    user_tags: pd.DataFrame,
    location_tags: pd.DataFrame,
    user_history: pd.DataFrame,
    user_actions: pd.DataFrame,
    config: PipelineConfig,
) -> pd.DataFrame:
    taste_scores, taste_details = _taste_contributions(user_tags, location_tags)

    top_trend_ids = (
        locations.sort_values(by="popularity_score", ascending=False)
        .head(250)["location_id"]
        .tolist()
    )
    top_hidden_ids = (
        locations.sort_values(by="hidden_gem_score", ascending=False)
        .head(250)["location_id"]
        .tolist()
    )

    user_ids = set(user_tags["user_id"].unique()) | set(user_history["user_id"].unique())
    if not user_actions.empty and "user_id" in user_actions.columns:
        user_ids.update(user_actions["user_id"].unique())
    candidate_map = _candidate_sets(taste_scores, top_trend_ids, top_hidden_ids, sorted(user_ids))
    seen_locations = _user_seen_locations(user_actions, locations)
    hist_lookup = user_history.set_index("user_id")["n_actions"].to_dict()
    location_metrics = locations.set_index("location_id")[[
        "name",
        "vicinity",
        "cuisine_primary",
        "popularity_score",
        "hidden_gem_score",
        "quality_score",
        "rating",
        "user_ratings_total",
    ]]

    recommendations: List[Dict] = []

    for user_id, candidate_ids in candidate_map.items():
        history_size = int(hist_lookup.get(user_id, len(candidate_ids) // 4))
        weights = _adaptive_weights(config.recommendation_weights, history_size)
        taste_subset = taste_scores[taste_scores["user_id"] == user_id].set_index("location_id")["taste_score"]
        seen = seen_locations.get(user_id, set())
        rows = []
        for loc_id in candidate_ids:
            if loc_id in seen:
                continue
            if loc_id not in location_metrics.index:
                continue
            metrics = location_metrics.loc[loc_id]
            taste_val = float(taste_subset.get(loc_id, 0.0))
            pop_val = float(metrics["popularity_score"])
            hidden_val = float(metrics["hidden_gem_score"])
            quality_val = float(metrics["quality_score"])
            score = (
                weights["taste"] * taste_val
                + weights["trend_app"] * pop_val
                + weights["hidden_gems"] * hidden_val
                + weights["quality"] * quality_val
            )
            reason = {
                "taste_tags": taste_details.get((user_id, loc_id), []),
                "weights": weights,
                "components": {
                    "taste": round(taste_val, 4),
                    "trend": round(pop_val, 4),
                    "hidden_gem": round(hidden_val, 4),
                    "quality": round(quality_val, 4),
                },
            }
            rows.append(
                {
                    "user_id": user_id,
                    "location_id": loc_id,
                    "score": score,
                    "taste_score": taste_val,
                    "trend_score": pop_val,
                    "hidden_gem_score": hidden_val,
                    "quality_score": quality_val,
                    "reason": json.dumps(reason, ensure_ascii=False),
                }
            )
        if not rows:
            continue
        rows = sorted(rows, key=lambda r: r["score"], reverse=True)
        for rank, row in enumerate(rows[: config.top_k_per_user], start=1):
            row["rank"] = rank
            recommendations.append(row)

    columns = [
        "user_id",
        "location_id",
        "rank",
        "score",
        "taste_score",
        "trend_score",
        "hidden_gem_score",
        "quality_score",
        "reason",
    ]
    return pd.DataFrame(recommendations, columns=columns)
