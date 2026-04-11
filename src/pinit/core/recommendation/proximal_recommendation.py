"""
Location-based (proximal) recommendation system.
Recommends venues within a specified radius of a given location, 
personalized to user taste profiles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

from pinit.config.settings import PipelineConfig


@dataclass
class ProximalConfig:
    """Configuration for proximal recommendations."""
    radius_km: float = 2.0  # Default 2km radius
    min_results: int = 10  # Minimum number of results to return
    max_results: int = 50  # Maximum number of results to return

    # Five-component weights
    quality_weight: float = 0.30
    vibe_weight: float = 0.25
    dietary_weight: float = 0.10
    social_weight: float = 0.20
    collaborative_weight: float = 0.15


def compute_adaptive_weights(
    has_friends: bool,
    action_count: int,
    quality_w: float = 0.30,
    vibe_w: float = 0.25,
    dietary_w: float = 0.10,
    social_w: float = 0.20,
    collaborative_w: float = 0.15,
) -> Dict[str, float]:
    """
    Rebalance scoring weights based on data availability.

    When a user has no friends, social weight is redistributed.
    When a user has few actions, collaborative and vibe weights are reduced
    in favour of quality (which works without user history).

    Returns:
        Dict with keys: quality, vibe, dietary, social, collaborative.
        Values sum to 1.0.
    """
    weights = {
        "quality": quality_w,
        "vibe": vibe_w,
        "dietary": dietary_w,
        "social": social_w,
        "collaborative": collaborative_w,
    }

    # No friends: redistribute social weight
    if not has_friends:
        redistributed = weights["social"]
        weights["social"] = 0.0
        weights["quality"] += redistributed * 0.4
        weights["vibe"] += redistributed * 0.3
        weights["collaborative"] += redistributed * 0.3

    # New user (few actions): reduce collaborative + vibe, boost quality
    if action_count < 5:
        cold_ratio = action_count / 5.0
        collab_reduction = weights["collaborative"] * (1 - cold_ratio)
        vibe_reduction = weights["vibe"] * (1 - cold_ratio) * 0.3
        weights["collaborative"] -= collab_reduction
        weights["vibe"] -= vibe_reduction
        weights["quality"] += collab_reduction + vibe_reduction

    # Normalize to sum to 1.0
    total = sum(weights.values())
    if total > 0:
        weights = {k: round(v / total, 4) for k, v in weights.items()}

    return weights


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great circle distance between two points on Earth.
    Returns distance in kilometers.
    
    Args:
        lat1, lon1: First point coordinates
        lat2, lon2: Second point coordinates
    
    Returns:
        Distance in kilometers
    """
    # Convert to radians
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    
    # Haversine formula
    a = (math.sin(delta_lat / 2) ** 2 + 
         math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2)
    c = 2 * math.asin(math.sqrt(a))
    
    # Earth's radius in kilometers
    r = 6371.0
    
    return c * r


def calculate_distances(
    center_lat: float,
    center_lon: float,
    locations: pd.DataFrame
) -> pd.Series:
    """
    Calculate distances from a center point to all locations.
    
    Args:
        center_lat: Center point latitude
        center_lon: Center point longitude
        locations: DataFrame with 'lat' and 'lng'/'lon' columns
    
    Returns:
        Series of distances in kilometers
    """
    # Handle both 'lng' and 'lon' column names
    lon_col = 'lng' if 'lng' in locations.columns else 'lon'
    
    distances = []
    for _, row in locations.iterrows():
        if pd.isna(row['lat']) or pd.isna(row[lon_col]):
            distances.append(float('inf'))
        else:
            dist = haversine_distance(
                center_lat, center_lon,
                float(row['lat']), float(row[lon_col])
            )
            distances.append(dist)
    
    return pd.Series(distances, index=locations.index)


def filter_by_radius(
    center_lat: float,
    center_lon: float,
    locations: pd.DataFrame,
    radius_km: float
) -> pd.DataFrame:
    """
    Filter locations within a specified radius.
    
    Args:
        center_lat: Center point latitude
        center_lon: Center point longitude
        locations: DataFrame with location data
        radius_km: Radius in kilometers
    
    Returns:
        Filtered DataFrame with 'distance_km' column added
    """
    distances = calculate_distances(center_lat, center_lon, locations)
    
    result = locations.copy()
    result['distance_km'] = distances
    result = result[result['distance_km'] <= radius_km]
    result = result.sort_values('distance_km')

    print(f"Found {len(result)} locations within {radius_km} km radius.")
    
    return result


def compute_vibe_score(
    user_id: str,
    location_ids: List[int],
    locations: pd.DataFrame
) -> pd.Series:
    """
    Compute vibe match scores using centered cosine similarity (Pearson correlation).

    Uses centered cosine to handle cold start gracefully - when user vector is uniform
    (e.g., all 50s), the centered vector becomes all zeros and similarity returns 0.0,
    allowing other ranking signals to dominate until user develops preferences.

    Args:
        user_id: User identifier
        location_ids: List of location IDs to score
        locations: DataFrame with location data including vibe_vector

    Returns:
        Series mapping location_id to vibe score (0-1)
    """
    from pinit.core.recommendation.vector_utils import centered_cosine_similarity
    from pinit.integrations.supabase import get_supabase_service
    import logging
    logger = logging.getLogger(__name__)

    # Fetch user data from database
    supabase = get_supabase_service()
    user_data = supabase.get_user(user_id)

    if not user_data:
        logger.debug(f"User {user_id} not found in database, returning 0.0 scores")
        return pd.Series(0.0, index=location_ids)

    # Get user's vibe affinity vector (column name: vibe_tag_affinity)
    user_vibe = user_data.get("vibe_tag_affinity")
    if not user_vibe:
        logger.debug(f"User {user_id} has no vibe_tag_affinity, returning 0.0 scores")
        return pd.Series(0.0, index=location_ids)

    scores = {}
    for loc_id in location_ids:
        loc_row = locations[locations['location_id'] == loc_id]
        if loc_row.empty:
            scores[loc_id] = 0.0
            continue

        loc_vibe = loc_row.iloc[0].get('vibe_vector')
        if not loc_vibe or not isinstance(loc_vibe, list):
            scores[loc_id] = 0.0
            continue

        scores[loc_id] = centered_cosine_similarity(user_vibe, loc_vibe)

    return pd.Series(scores)


def compute_dietary_score(
    user_id: str,
    location_ids: List[int],
    locations: pd.DataFrame
) -> pd.Series:
    """
    Compute dietary requirement match scores using dot product.

    Args:
        user_id: User identifier
        location_ids: List of location IDs to score
        locations: DataFrame with location data including dietary_requirement_vector

    Returns:
        Series mapping location_id to dietary score (0-1)
    """
    from pinit.core.recommendation.vector_utils import dot_product
    from pinit.integrations.supabase import get_supabase_service
    import logging
    logger = logging.getLogger(__name__)

    # Fetch user data from database
    supabase = get_supabase_service()
    user_data = supabase.get_user(user_id)

    if not user_data:
        logger.debug(f"User {user_id} not found in database, returning 0.0 scores")
        return pd.Series(0.0, index=location_ids)

    # Get user's dietary affinity vector (column name: dietary_requirement_tag_affinity)
    user_dietary = user_data.get("dietary_requirement_tag_affinity")
    if not user_dietary:
        logger.debug(f"User {user_id} has no dietary_requirement_tag_affinity, returning 0.0 scores")
        return pd.Series(0.0, index=location_ids)

    scores = {}
    for loc_id in location_ids:
        loc_row = locations[locations['location_id'] == loc_id]
        if loc_row.empty:
            scores[loc_id] = 0.0
            continue

        loc_dietary = loc_row.iloc[0].get('dietary_requirement_vector')
        if not loc_dietary or not isinstance(loc_dietary, list):
            scores[loc_id] = 0.0
            continue

        scores[loc_id] = dot_product(user_dietary, loc_dietary)

    return pd.Series(scores)


def build_proximal_recommendations(
    user_id: str,
    center_lat: float,
    center_lon: float,
    locations: pd.DataFrame,
    user_tags: pd.DataFrame,
    location_tags: pd.DataFrame,
    config: Optional[ProximalConfig] = None,
    cuisine_filters: Optional[List[str]] = None,
    vibe_filters: Optional[List[str]] = None,
    filter_threshold: float = 40.0,
    skip_filters: bool = False,
    include_tag_data: bool = False,
    compute_proximity: bool = True
) -> pd.DataFrame:
    """
    Generate personalized recommendations within a geographic radius.

    Args:
        user_id: User identifier
        center_lat: Center point latitude
        center_lon: Center point longitude
        locations: Full location inventory
        user_tags: User taste profiles (deprecated, kept for compatibility)
        location_tags: Location-tag associations (deprecated, kept for compatibility)
        config: Configuration parameters
        cuisine_filters: Optional list of cuisine tag IDs (OR logic)
        vibe_filters: Optional list of vibe tag IDs (AND logic)
        filter_threshold: Score threshold for filters (default 40.0)
        skip_filters: If True, skip cuisine/vibe filtering (for caching)
        include_tag_data: If True, include complete tag data for each location
        compute_proximity: If True, compute proximity scores (deprecated - not used in final score)

    Returns:
        DataFrame with ranked recommendations including:
        - location_id, name, distance_km
        - vibe_score, dietary_score, quality_score, final_score
        - rank
        - (if include_tag_data=True) cuisine_tags, vibe_tags, all_tags
    """
    if config is None:
        config = ProximalConfig()

    # Filter locations by radius
    nearby = filter_by_radius(center_lat, center_lon, locations, config.radius_km)

    if nearby.empty:
        # If nothing in radius, expand search
        nearby = filter_by_radius(center_lat, center_lon, locations, config.radius_km * 2)

    if nearby.empty:
        return pd.DataFrame(columns=[
            'location_id', 'name', 'distance_km', 'vibe_score',
            'dietary_score', 'quality_score', 'final_score', 'rank'
        ])

    # Apply optional filters (cuisine OR, vibe AND) unless skip_filters is True
    if not skip_filters and (cuisine_filters or vibe_filters):
        from pinit.core.recommendation.bubble_recommendation import _filter_candidates_by_tags
        nearby = _filter_candidates_by_tags(
            nearby,
            cuisine_filters=cuisine_filters,
            vibe_filters=vibe_filters,
        )

        if nearby.empty:
            return pd.DataFrame(columns=[
                'location_id', 'name', 'distance_km', 'vibe_score',
                'dietary_score', 'quality_score', 'final_score', 'rank'
            ])

    # Compute component scores
    location_ids = nearby['location_id'].tolist()

    vibe_scores = compute_vibe_score(user_id, location_ids, locations)
    dietary_scores = compute_dietary_score(user_id, location_ids, locations)
    # quality_score is precomputed by the v4 cron and surfaced via the RPC,
    # so it should already be on the nearby DataFrame. Fall back to 0.0 if a
    # caller passed in a DataFrame that predates v4 (e.g. demo scripts).
    if 'quality_score' in nearby.columns:
        quality_scores = nearby['quality_score'].fillna(0.0)
    else:
        quality_scores = pd.Series(0.0, index=nearby.index)

    # Compute social and collaborative scores
    from pinit.core.recommendation.social_scoring import compute_social_scores
    from pinit.core.recommendation.collaborative_scoring import compute_collaborative_scores
    from pinit.integrations.supabase import get_supabase_service

    supabase = get_supabase_service()
    social_scores_dict, _ = compute_social_scores(user_id, location_ids, supabase)
    collab_scores_dict = compute_collaborative_scores(user_id, location_ids, supabase)

    # Adaptive weights
    has_friends = any(s > 0 for s in social_scores_dict.values())
    action_count = supabase.get_user_action_count(user_id)
    weights = compute_adaptive_weights(
        has_friends=has_friends,
        action_count=action_count,
        quality_w=config.quality_weight,
        vibe_w=config.vibe_weight,
        dietary_w=config.dietary_weight,
        social_w=config.social_weight,
        collaborative_w=config.collaborative_weight,
    )

    # Combine scores
    nearby_copy = nearby.copy()
    nearby_copy['vibe_score'] = nearby_copy['location_id'].map(vibe_scores)
    nearby_copy['dietary_score'] = nearby_copy['location_id'].map(dietary_scores)
    nearby_copy['quality_score'] = quality_scores.values
    nearby_copy['social_score'] = nearby_copy['location_id'].map(social_scores_dict).fillna(0.0)
    nearby_copy['collaborative_score'] = nearby_copy['location_id'].map(collab_scores_dict).fillna(0.0)

    # Calculate final weighted score with all 5 components
    nearby_copy['final_score'] = (
        weights["quality"] * nearby_copy['quality_score'] +
        weights["vibe"] * nearby_copy['vibe_score'] +
        weights["dietary"] * nearby_copy['dietary_score'] +
        weights["social"] * nearby_copy['social_score'] +
        weights["collaborative"] * nearby_copy['collaborative_score']
    )
    
    # Sort by final score
    nearby_copy = nearby_copy.sort_values('final_score', ascending=False)
    
    # Add rank
    nearby_copy['rank'] = range(1, len(nearby_copy) + 1)

    # Include tag data if requested (for caching)
    if include_tag_data:
        # Initialize columns as object dtype to store lists
        nearby_copy['cuisine_tags'] = None
        nearby_copy['vibe_tags'] = None
        nearby_copy['all_tags'] = None

        # Get tags for each location
        for idx, row in nearby_copy.iterrows():
            location_id = row['location_id']
            loc_tags = location_tags[location_tags['location_id'] == location_id]

            # Separate cuisine and vibe tags
            cuisine_tags = loc_tags[loc_tags['tag_id'].str.startswith('cuisine_')]['tag_text'].tolist()
            vibe_tags = loc_tags[~loc_tags['tag_id'].str.startswith('cuisine_')]['tag_text'].tolist()
            all_tags = loc_tags[['tag_id', 'tag_text', 'score']].to_dict('records')

            # Store as objects (lists/dicts) for caching
            nearby_copy.at[idx, 'cuisine_tags'] = cuisine_tags
            nearby_copy.at[idx, 'vibe_tags'] = vibe_tags
            nearby_copy.at[idx, 'all_tags'] = all_tags

    # Limit results
    result = nearby_copy.head(config.max_results)
    
    # Ensure minimum results if possible
    if len(result) < config.min_results and len(nearby_copy) < config.min_results:
        # Expand radius if we don't have enough results
        expanded = filter_by_radius(
            center_lat, center_lon, locations, config.radius_km * 3
        )
        if len(expanded) > len(nearby):
            # Recursively call with expanded radius
            expanded_config = ProximalConfig(
                radius_km=config.radius_km * 3,
                min_results=config.min_results,
                max_results=config.max_results,
                quality_weight=config.quality_weight,
                vibe_weight=config.vibe_weight,
                dietary_weight=config.dietary_weight,
                social_weight=config.social_weight,
                collaborative_weight=config.collaborative_weight,
            )
            return build_proximal_recommendations(
                user_id, center_lat, center_lon, locations,
                user_tags, location_tags, expanded_config
            )
    
    # Ensure consistent column naming (lng vs lon)
    if 'lon' in result.columns and 'lng' not in result.columns:
        result['lng'] = result['lon']

    # Select key columns for output
    output_cols = [
        'location_id', 'name', 'vicinity', 'cuisine_primary',
        'rating', 'user_ratings_total', 'price_level',
        'lat', 'lng',
        'distance_km', 'vibe_score', 'dietary_score', 'quality_score',
        'social_score', 'collaborative_score', 'final_score', 'rank'
    ]

    # Only include columns that exist
    available_cols = [col for col in output_cols if col in result.columns]
    result = result[available_cols].reset_index(drop=True)

    # Replace NaN with None for proper JSON serialization
    # This prevents Pydantic validation errors when NaN is passed as a string field
    result = result.replace({np.nan: None})

    return result


def build_batch_proximal_recommendations(
    user_ids: List[str],
    center_lat: float,
    center_lon: float,
    locations: pd.DataFrame,
    user_tags: pd.DataFrame,
    location_tags: pd.DataFrame,
    config: Optional[ProximalConfig] = None
) -> pd.DataFrame:
    """
    Generate proximal recommendations for multiple users.

    Args:
        user_ids: List of user identifiers
        center_lat: Center point latitude
        center_lon: Center point longitude
        locations: Full location inventory
        user_tags: User taste profiles (deprecated, kept for compatibility)
        location_tags: Location-tag associations (deprecated, kept for compatibility)
        config: Configuration parameters

    Returns:
        Combined DataFrame with recommendations for all users
    """
    all_recs = []

    for user_id in user_ids:
        user_recs = build_proximal_recommendations(
            user_id, center_lat, center_lon,
            locations, user_tags, location_tags, config
        )
        if not user_recs.empty:
            user_recs['user_id'] = user_id
            all_recs.append(user_recs)

    if not all_recs:
        return pd.DataFrame(columns=['user_id', 'location_id'])

    return pd.concat(all_recs, ignore_index=True)


def get_location_coordinates(
    location_id: int,
    locations: pd.DataFrame
) -> Optional[Tuple[float, float]]:
    """
    Get coordinates for a specific location.
    
    Args:
        location_id: Location identifier
        locations: Location inventory
    
    Returns:
        Tuple of (latitude, longitude) or None if not found
    """
    loc = locations[locations['location_id'] == location_id]
    
    if loc.empty:
        return None
    
    lon_col = 'lng' if 'lng' in loc.columns else 'lon'
    lat = loc.iloc[0]['lat']
    lon = loc.iloc[0][lon_col]
    
    if pd.isna(lat) or pd.isna(lon):
        return None
    
    return (float(lat), float(lon))
