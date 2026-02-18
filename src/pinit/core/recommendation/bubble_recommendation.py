"""
Group (bubble) recommendation system with context-aware aggregation.

Flow:
1. Filter by geographic radius
2. OPTIONAL-FILTER by cuisine (OR) and vibe (AND)
3. Compute individual vibe scores for each user (centered cosine similarity)
4. Compute individual dietary scores for each user (dot product)
5. Aggregate vibe using context-aware strategy
6. Aggregate dietary using MAX pooling
7. Combine with quality
8. Diversify across group's top vibe tags
9. Rank and return
"""

from __future__ import annotations

from typing import Dict, List, Optional
import pandas as pd
import numpy as np
from dataclasses import dataclass
import logging

from pinit.core.recommendation.proximal_recommendation import (
    filter_by_radius,
    compute_quality_score,
)
from pinit.core.recommendation.vector_utils import centered_cosine_similarity, dot_product
from pinit.integrations.supabase import get_supabase_service

logger = logging.getLogger(__name__)


@dataclass
class BubbleConfig:
    """Configuration for bubble recommendations."""
    radius_km: float = 2.0
    max_results: int = 20
    vibe_weight: float = 0.34
    dietary_weight: float = 0.33
    quality_weight: float = 0.33
    activity_threshold: int = 20  # Min actions to be "active"
    filter_threshold: float = 50.0
    variance_threshold: float = 0.15  # High disagreement threshold


VIBE_TAG_ORDER = {
    "cafe": 0, "casual": 1, "cozy": 2, "coffee_shop": 3, "bar": 4,
    "elegant": 5, "fine_dining": 6, "food_truck": 7, "hole_in_the_wall": 8, "late_night": 9,
    "live_music": 10, "michelin_starred": 11, "modern": 12, "fast_food": 13, "quiet": 14,
    "romantic": 15, "sports_bar": 16, "trendy": 17, "takeout_friendly": 18, "pub": 19,
    "grocery_store": 20, "brunch": 21, "outdoor_dining": 22, "wavy": 23, "bossman": 24,
}


def compute_user_activity_scores(
    user_ids: List[str],
    activity_threshold: int = 20
) -> Dict[str, float]:
    """
    Compute activity weights based on user action counts.
    - < 20 actions: scaled weight (0.1 to 1.0)
    - >= 20 actions: full weight (1.0)
    """
    supabase = get_supabase_service()

    activity_scores = {}

    for user_id in user_ids:
        try:
            action_count = supabase.get_user_action_count(user_id)

            if action_count >= activity_threshold:
                activity_scores[user_id] = 1.0
            else:
                activity_scores[user_id] = max(0.1, action_count / activity_threshold)

        except Exception as exc:
            logger.warning(f"Could not fetch actions for {user_id}: {exc}")
            activity_scores[user_id] = 0.1

    # Normalize to sum to 1.0
    total = sum(activity_scores.values())
    if total > 0:
        activity_scores = {uid: score/total for uid, score in activity_scores.items()}
    else:
        activity_scores = {uid: 1.0/len(user_ids) for uid in user_ids}

    return activity_scores


def compute_individual_vibe_scores(
    user_ids: List[str],
    location_ids: List[int],
    locations: pd.DataFrame
) -> pd.DataFrame:
    """
    Compute vibe scores using centered cosine similarity for each user-location pair.
    Returns DataFrame with: user_id, location_id, vibe_score
    """
    supabase = get_supabase_service()
    results = []

    for user_id in user_ids:
        user_data = supabase.get_user(user_id)
        user_vibe = user_data.get("vibe_tag_affinity") if user_data else None

        for loc_id in location_ids:
            if not user_vibe:
                results.append({'user_id': user_id, 'location_id': loc_id, 'vibe_score': 0.0})
                continue

            loc_row = locations[locations['location_id'] == loc_id]
            if loc_row.empty:
                results.append({'user_id': user_id, 'location_id': loc_id, 'vibe_score': 0.0})
                continue

            loc_vibe = loc_row.iloc[0].get('vibe_vector')
            if not loc_vibe or not isinstance(loc_vibe, list):
                results.append({'user_id': user_id, 'location_id': loc_id, 'vibe_score': 0.0})
                continue

            score = centered_cosine_similarity(user_vibe, loc_vibe)
            results.append({'user_id': user_id, 'location_id': loc_id, 'vibe_score': score})

    return pd.DataFrame(results)


def compute_individual_dietary_scores(
    user_ids: List[str],
    location_ids: List[int],
    locations: pd.DataFrame
) -> pd.DataFrame:
    """
    Compute dietary scores using dot product for each user-location pair.
    Returns DataFrame with: user_id, location_id, dietary_score
    """
    supabase = get_supabase_service()
    results = []

    for user_id in user_ids:
        user_data = supabase.get_user(user_id)
        user_dietary = user_data.get("dietary_requirement_tag_affinity") if user_data else None

        for loc_id in location_ids:
            if not user_dietary:
                results.append({'user_id': user_id, 'location_id': loc_id, 'dietary_score': 0.0})
                continue

            loc_row = locations[locations['location_id'] == loc_id]
            if loc_row.empty:
                results.append({'user_id': user_id, 'location_id': loc_id, 'dietary_score': 0.0})
                continue

            loc_dietary = loc_row.iloc[0].get('dietary_requirement_vector')
            if not loc_dietary or not isinstance(loc_dietary, list):
                results.append({'user_id': user_id, 'location_id': loc_id, 'dietary_score': 0.0})
                continue

            score = dot_product(user_dietary, loc_dietary)
            results.append({'user_id': user_id, 'location_id': loc_id, 'dietary_score': score})

    return pd.DataFrame(results)


def aggregate_vibe_scores_context_aware(
    individual_scores: pd.DataFrame,
    activity_scores: Dict[str, float],
    variance_threshold: float = 0.15
) -> pd.DataFrame:
    """
    Context-aware aggregation:
    When there is a small group (2 users), we want to avoid recommending something that one user strongly dislikes, even if the other user loves it so:
    If group size == 2 -> min(scores)

    For larger groups we make a decision dependant on how much disagreement there is:
        - If there is low disagreement (variance <= threshold), we can just trust the average (because users mostly agree).
        - If there is high disagreement (variance > threshold), we want to give more weight to the minimum score (to avoid recommending something that one user strongly dislikes).

    Therefore:
    - Variance > 0.15: 0.4 x min + 0.6 x weighted_avg
    - Variance <= 0.15: weighted_avg
    """
    results = []

    for location_id in individual_scores['location_id'].unique():
        loc_scores = individual_scores[individual_scores['location_id'] == location_id]

        scores_array = loc_scores['vibe_score'].values
        user_ids = loc_scores['user_id'].tolist()
        group_size = len(scores_array)

        weights = np.array([activity_scores.get(uid, 1.0/group_size) for uid in user_ids])
        variance = scores_array.var()

        # Apply context-aware logic
        if group_size == 2:
            group_vibe_score = scores_array.min()
        elif variance > variance_threshold:
            min_score = scores_array.min()
            avg_score = (scores_array * weights).sum()
            group_vibe_score = 0.4 * min_score + 0.6 * avg_score
        else:
            group_vibe_score = (scores_array * weights).sum()

        results.append({
            'location_id': location_id,
            'group_vibe_score': group_vibe_score,
            'min_individual_score': scores_array.min(),
            'max_individual_score': scores_array.max(),
            'score_variance': variance,
        })

    return pd.DataFrame(results)


def aggregate_dietary_scores_max(
    individual_dietary_scores: pd.DataFrame,
) -> pd.DataFrame:
    """
    MAX pool dietary scores across users per location.
    Returns DataFrame with: location_id, group_dietary_score
    """
    grouped = individual_dietary_scores.groupby('location_id')['dietary_score'].max().reset_index()
    grouped.columns = ['location_id', 'group_dietary_score']
    return grouped


def compute_group_vibe_vector(
    user_ids: List[str],
    activity_scores: Dict[str, float],
) -> Optional[List[float]]:
    """
    Compute activity-weighted average of all users' vibe vectors.
    Returns a single vector representing the group's blended vibe preferences.
    """
    supabase = get_supabase_service()
    vectors = []
    weights = []

    for user_id in user_ids:
        user_data = supabase.get_user(user_id)
        if not user_data:
            continue
        vibe_vec = user_data.get("vibe_tag_affinity")
        if not vibe_vec:
            continue
        vectors.append(np.array(vibe_vec, dtype=float))
        weights.append(activity_scores.get(user_id, 1.0 / len(user_ids)))

    if not vectors:
        return None

    weights_arr = np.array(weights)
    # Normalize weights to sum to 1
    weights_arr = weights_arr / weights_arr.sum()

    group_vec = np.zeros_like(vectors[0])
    for vec, w in zip(vectors, weights_arr):
        group_vec += w * vec

    return group_vec.tolist()


def diversify_by_group_tags(
    scored_df: pd.DataFrame,
    group_vibe_vec: List[float],
    max_results: int,
    top_n_tags: int = 5,
) -> pd.DataFrame:
    """
    Apply proportional diversity sampling across the group's top vibe tags.

    Allocates result slots to each of the group's top N tags proportionally
    to their affinity scores. Within each bucket, picks candidates with the
    highest tag-specific affinity blended with final_score. Re-sorts by final_score.
    """
    index_to_tag = {idx: tag for tag, idx in VIBE_TAG_ORDER.items()}

    # Extract group's top N tags (by affinity score, skip zeros)
    user_arr = np.array(group_vibe_vec, dtype=float)
    tag_scores = [
        (index_to_tag[i], float(user_arr[i]))
        for i in range(len(user_arr))
        if i in index_to_tag and float(user_arr[i]) > 0
    ]
    tag_scores.sort(key=lambda x: x[1], reverse=True)
    top_tags = tag_scores[:top_n_tags]

    if not top_tags:
        return scored_df.head(max_results)

    total_weight = sum(score for _, score in top_tags)

    # Compute proportional slot counts using largest-remainder method
    raw_slots = [(tag, score / total_weight * max_results) for tag, score in top_tags]
    floor_slots = {tag: int(s) for tag, s in raw_slots}
    remainder = max_results - sum(floor_slots.values())
    fractional_order = sorted(
        raw_slots, key=lambda x: x[1] - int(x[1]), reverse=True
    )
    for i in range(remainder):
        floor_slots[fractional_order[i][0]] += 1
    slot_map = floor_slots

    selected_ids = set()
    result_indices = []

    for tag_name, slot_count in slot_map.items():
        if slot_count == 0:
            continue
        tag_idx = VIBE_TAG_ORDER.get(tag_name)
        if tag_idx is None:
            continue

        # Score each un-selected candidate by tag-specific affinity blended with final_score
        available = scored_df[~scored_df['location_id'].isin(selected_ids)].copy()
        if available.empty:
            continue

        def _tag_affinity(row, idx=tag_idx):
            loc_vec = row.get('vibe_vector')
            if not loc_vec or not isinstance(loc_vec, list) or idx >= len(loc_vec):
                tag_raw = 0.0
            else:
                tag_raw = float(loc_vec[idx]) / 100.0
            return 0.7 * tag_raw + 0.3 * row.get('final_score', 0.0)

        available['_tag_affinity'] = available.apply(_tag_affinity, axis=1)
        available = available.sort_values('_tag_affinity', ascending=False)

        picked = available.head(slot_count)
        selected_ids.update(picked['location_id'].tolist())
        result_indices.extend(picked.index.tolist())

    # Fill remaining slots
    if len(result_indices) < max_results:
        remaining = scored_df[~scored_df['location_id'].isin(selected_ids)]
        result_indices.extend(remaining.head(max_results - len(result_indices)).index.tolist())

    result = scored_df.loc[result_indices]
    result = result.sort_values('final_score', ascending=False).head(max_results)
    return result.reset_index(drop=True)


def _filter_candidates_by_tags(
    locations_df: pd.DataFrame,
    cuisine_filters: Optional[List[str]] = None,
    vibe_filters: Optional[List[str]] = None,
) -> pd.DataFrame:
    """
    Filter locations by cuisine and vibe tags using vector-based approach.

    Cuisine filter (OR logic): cuisine_primary or cuisine_secondary matches.
    Vibe filter (AND logic): vibe_vector value at tag index must be > 50.
    """
    if not cuisine_filters and not vibe_filters:
        return locations_df

    mask = pd.Series(True, index=locations_df.index)

    # Cuisine filter (OR logic)
    if cuisine_filters:
        cuisine_set = {c.lower() for c in cuisine_filters}

        def _cuisine_match(row):
            primary = str(row.get('cuisine_primary') or '').lower()
            secondary = str(row.get('cuisine_secondary') or '').lower()
            return primary in cuisine_set or secondary in cuisine_set

        mask &= locations_df.apply(_cuisine_match, axis=1)

    # Vibe filter (AND logic)
    if vibe_filters:
        vibe_indices = [VIBE_TAG_ORDER[v] for v in vibe_filters if v in VIBE_TAG_ORDER]

        def _vibe_match(row):
            vibe_vec = row.get('vibe_vector')
            if not vibe_vec or not isinstance(vibe_vec, list):
                return False
            return all(
                idx < len(vibe_vec) and (vibe_vec[idx] or 0) > 50
                for idx in vibe_indices
            )

        if vibe_indices:
            mask &= locations_df.apply(_vibe_match, axis=1)

    return locations_df[mask]


def build_bubble_recommendations(
    user_ids: List[str],
    center_lat: float,
    center_lon: float,
    locations: pd.DataFrame,
    cuisine_filters: Optional[List[str]] = None,
    vibe_filters: Optional[List[str]] = None,
    config: Optional[BubbleConfig] = None
) -> pd.DataFrame:
    """Build group recommendations with vector-based scoring and context-aware aggregation."""
    if config is None:
        config = BubbleConfig()

    # 1. Geographic filter
    nearby = filter_by_radius(center_lat, center_lon, locations, config.radius_km)
    if nearby.empty:
        return pd.DataFrame()

    locations_before = len(nearby)
    logger.info(f"Found {locations_before} locations within {config.radius_km} km radius for users {user_ids}")

    # 2. Optional filters (cuisine OR, vibe AND) — vector-based
    nearby = _filter_candidates_by_tags(nearby, cuisine_filters, vibe_filters)
    if nearby.empty:
        return pd.DataFrame()

    locations_after = len(nearby)
    logger.info(f"{locations_after} locations remain after applying optional filters: cuisine={cuisine_filters}, vibe={vibe_filters}")

    # 3. Activity scores
    activity_scores = compute_user_activity_scores(user_ids, config.activity_threshold)

    # 4. Individual vibe scores
    location_ids = nearby['location_id'].tolist()
    individual_vibe = compute_individual_vibe_scores(user_ids, location_ids, locations)

    # 5. Individual dietary scores
    individual_dietary = compute_individual_dietary_scores(user_ids, location_ids, locations)

    # 6. Aggregate vibe (context-aware)
    group_vibe = aggregate_vibe_scores_context_aware(
        individual_vibe, activity_scores, config.variance_threshold
    )

    # 7. Aggregate dietary (MAX pool)
    group_dietary = aggregate_dietary_scores_max(individual_dietary)

    # 8. Quality scores
    quality_scores = compute_quality_score(nearby)

    # 9. Merge
    result = nearby.copy()
    result = result.merge(group_vibe, on='location_id', how='left')
    result = result.merge(group_dietary, on='location_id', how='left')
    result['quality_score'] = quality_scores.values

    # 10. Final score
    result['final_score'] = (
        config.vibe_weight * result['group_vibe_score'].fillna(0) +
        config.dietary_weight * result['group_dietary_score'].fillna(0) +
        config.quality_weight * result['quality_score']
    )

    # 11. Sort and rank
    result = result.sort_values('final_score', ascending=False)
    result['rank'] = range(1, len(result) + 1)

    # 12. Diversify across group's top vibe tags
    group_vibe_vec = compute_group_vibe_vector(user_ids, activity_scores)
    if group_vibe_vec:
        result = diversify_by_group_tags(result, group_vibe_vec, config.max_results)
        # Re-rank after diversification
        result['rank'] = range(1, len(result) + 1)
    else:
        result = result.head(config.max_results)

    # 13. Attach individual scores (vibe + dietary per user)
    # Merge individual vibe and dietary into a combined per-user record
    individual_combined = individual_vibe.merge(
        individual_dietary, on=['user_id', 'location_id'], how='outer'
    ).fillna(0.0)

    result['individual_scores'] = result['location_id'].apply(
        lambda loc_id: individual_combined[individual_combined['location_id'] == loc_id][
            ['user_id', 'vibe_score', 'dietary_score']
        ].to_dict('records')
    )

    result['individual_scores'] = result['individual_scores'].apply(
        lambda scores: [
            {**score, 'activity_weight': activity_scores.get(score['user_id'], 0)}
            for score in scores
        ]
    )

    # 14. Metadata
    result['locations_before_filtering'] = locations_before
    result['locations_after_filtering'] = locations_after

    # 15. Output columns
    output_cols = [
        'location_id', 'name', 'vicinity', 'cuisine_primary',
        'rating', 'user_ratings_total', 'price_level',
        'distance_km', 'group_vibe_score', 'group_dietary_score',
        'quality_score', 'final_score', 'rank',
        'individual_scores', 'min_individual_score',
        'max_individual_score', 'score_variance',
        'locations_before_filtering', 'locations_after_filtering'
    ]

    available_cols = [col for col in output_cols if col in result.columns]
    result = result[available_cols].reset_index(drop=True)

    # Replace NaN with None for proper JSON serialization
    result = result.replace({np.nan: None})

    return result
