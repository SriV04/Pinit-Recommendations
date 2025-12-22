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

from config import PipelineConfig


@dataclass
class ProximalConfig:
    """Configuration for proximal recommendations."""
    radius_km: float = 2.0  # Default 2km radius
    min_results: int = 10  # Minimum number of results to return
    max_results: int = 50  # Maximum number of results to return
    taste_weight: float = 0.2  # Weight for user taste matching
    proximity_weight: float = 0.6  # Weight for proximity (closer is better)
    quality_weight: float = 0.2  # Weight for quality metrics


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


def compute_taste_score(
    user_id: str,
    location_ids: List[int],
    user_tags: pd.DataFrame,
    location_tags: pd.DataFrame
) -> pd.Series:
    """
    Compute taste match scores for locations.
    
    Args:
        user_id: User identifier
        location_ids: List of location IDs to score
        user_tags: User taste profile
        location_tags: Location tag associations
    
    Returns:
        Series mapping location_id to taste score (0-1)
    """
    user_profile = user_tags[user_tags['user_id'] == user_id]
    
    if user_profile.empty:
        return pd.Series(0.0, index=location_ids)
    
    # Get relevant location tags
    relevant_tags = location_tags[location_tags['location_id'].isin(location_ids)]
    
    if relevant_tags.empty:
        return pd.Series(0.0, index=location_ids)
    
    # Merge user preferences with location tags
    merged = user_profile.merge(
        relevant_tags[['location_id', 'tag_id', 'score']],
        on='tag_id',
        how='inner'
    )
    
    if merged.empty:
        return pd.Series(0.0, index=location_ids)
    
    # Calculate taste score as weighted overlap
    merged['component'] = (merged['score_x'] / 100.0) * (merged['score_y'] / 100.0)
    
    taste_scores = (
        merged.groupby('location_id')['component']
        .sum()
        .clip(upper=1.0)  # Cap at 1.0
    )
    
    # Fill missing locations with 0
    return pd.Series(taste_scores).reindex(location_ids, fill_value=0.0)


def compute_proximity_score(distances: pd.Series, max_distance: float) -> pd.Series:
    """
    Compute proximity score (1.0 for very close, decreasing with distance).
    
    Args:
        distances: Series of distances in km
        max_distance: Maximum distance considered (radius)
    
    Returns:
        Series of proximity scores (0-1)
    """
    # Exponential decay: closer is much better
    return np.exp(-2 * distances / max_distance)


def compute_quality_score(locations: pd.DataFrame) -> pd.Series:
    """
    Compute quality score based on ratings and review counts.
    
    Args:
        locations: DataFrame with 'rating' and 'user_ratings_total' columns
    
    Returns:
        Series of quality scores (0-1)
    """
    # Normalize rating (assuming 0-5 scale)
    rating_score = locations['rating'].fillna(3.0) / 5.0
    
    # Log-scale review count (more reviews = more reliable)
    review_score = np.log1p(locations['user_ratings_total'].fillna(0)) / 10.0
    review_score = review_score.clip(upper=1.0)
    
    # Combine: 70% rating, 30% review reliability
    quality = (0.7 * rating_score + 0.3 * review_score).clip(upper=1.0)
    
    return quality


def build_proximal_recommendations(
    user_id: str,
    center_lat: float,
    center_lon: float,
    locations: pd.DataFrame,
    user_tags: pd.DataFrame,
    location_tags: pd.DataFrame,
    config: Optional[ProximalConfig] = None
) -> pd.DataFrame:
    """
    Generate personalized recommendations within a geographic radius.
    
    Args:
        user_id: User identifier
        center_lat: Center point latitude
        center_lon: Center point longitude
        locations: Full location inventory
        user_tags: User taste profiles
        location_tags: Location-tag associations
        config: Configuration parameters
    
    Returns:
        DataFrame with ranked recommendations including:
        - location_id, name, distance_km
        - taste_score, proximity_score, quality_score, final_score
        - rank
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
            'location_id', 'name', 'distance_km', 'taste_score',
            'proximity_score', 'quality_score', 'final_score', 'rank'
        ])
    
    # Compute component scores
    taste_scores = compute_taste_score(
        user_id,
        nearby['location_id'].tolist(),
        user_tags,
        location_tags
    )
    
    proximity_scores = compute_proximity_score(
        nearby['distance_km'],
        config.radius_km
    )
    
    quality_scores = compute_quality_score(nearby)
    
    # Combine scores
    nearby_copy = nearby.copy()
    nearby_copy['taste_score'] = nearby_copy['location_id'].map(taste_scores)
    nearby_copy['proximity_score'] = proximity_scores.values
    nearby_copy['quality_score'] = quality_scores.values
    
    # Calculate final weighted score
    nearby_copy['final_score'] = (
        config.taste_weight * nearby_copy['taste_score'] +
        config.proximity_weight * nearby_copy['proximity_score'] +
        config.quality_weight * nearby_copy['quality_score']
    )
    
    # Sort by final score
    nearby_copy = nearby_copy.sort_values('final_score', ascending=False)
    
    # Add rank
    nearby_copy['rank'] = range(1, len(nearby_copy) + 1)
    
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
                taste_weight=config.taste_weight,
                proximity_weight=config.proximity_weight,
                quality_weight=config.quality_weight
            )
            return build_proximal_recommendations(
                user_id, center_lat, center_lon, locations,
                user_tags, location_tags, expanded_config
            )
    
    # Select key columns for output
    output_cols = [
        'location_id', 'name', 'vicinity', 'cuisine_primary',
        'rating', 'user_ratings_total', 'price_level',
        'distance_km', 'taste_score', 'proximity_score',
        'quality_score', 'final_score', 'rank'
    ]
    
    # Only include columns that exist
    available_cols = [col for col in output_cols if col in result.columns]
    
    return result[available_cols].reset_index(drop=True)


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
        user_tags: User taste profiles
        location_tags: Location-tag associations
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
