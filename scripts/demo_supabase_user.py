"""
Demo script for proximal recommendations using a real Supabase user.
Shows how to get personalized recommendations for an actual user from the database.
"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pandas as pd

from pinit.config.settings import PipelineConfig, PipelinePaths, ReviewTagConfig
from pinit.core.recommendation.tag_taxonomy import get_tags_dataframe
from pinit.core.recommendation.static_tagging import load_locations, load_reviews, build_location_tags
from pinit.core.recommendation.user_profiles import (
    load_user_tag_affinities_from_supabase,
    convert_supabase_affinities_to_profile_format
)
from pinit.core.recommendation.proximal_recommendation import (
    build_proximal_recommendations,
    ProximalConfig
)


def print_section(title: str):
    """Print a formatted section header."""
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}\n")


def display_user_profile(user_tags: pd.DataFrame, user_id: str, top_n: int = 10):
    """Display a user's top tag preferences."""
    user_prefs = user_tags[user_tags['user_id'] == user_id].head(top_n)
    
    if user_prefs.empty:
        print(f"⚠️  No preferences found for user {user_id}")
        return
    
    print(f"👤 User Profile: {user_id}")
    print(f"   Top {len(user_prefs)} preferences:")
    print()
    
    for idx, row in user_prefs.iterrows():
        tag = row['tag_text']
        score = row['score']
        print(f"   • {tag:20s} {score:5.1f}/100")


def get_taste_breakdown(location_id: int, user_id: str, user_tags: pd.DataFrame, 
                        location_tags: pd.DataFrame, top_n: int = 5):
    """
    Get the breakdown of which tags contributed to the taste score.
    
    Returns:
        List of (tag_text, user_score, location_score, contribution) tuples
    """
    user_profile = user_tags[user_tags['user_id'] == user_id]
    loc_tags = location_tags[location_tags['location_id'] == location_id]
    
    if user_profile.empty or loc_tags.empty:
        return []
    
    # Merge to find matching tags
    merged = user_profile.merge(
        loc_tags[['tag_id', 'tag_text', 'score']],
        on='tag_id',
        how='inner',
        suffixes=('_user', '_loc')
    )
    
    if merged.empty:
        return []
    
    # Calculate contribution
    merged['contribution'] = (merged['score_user'] / 100.0) * (merged['score_loc'] / 100.0)
    merged = merged.sort_values('contribution', ascending=False)
    
    results = []
    for _, row in merged.head(top_n).iterrows():
        # Use either tag_text_user or tag_text_loc (they should be the same)
        tag_name = row.get('tag_text_user', row.get('tag_text_loc', 'unknown'))
        results.append((
            tag_name,
            row['score_user'],  # user score
            row['score_loc'],  # location score
            row['contribution']
        ))
    
    return results


def display_recommendations(recs: pd.DataFrame, user_id: str, user_tags: pd.DataFrame,
                           location_tags: pd.DataFrame, title: str = "Recommendations"):
    """Display recommendations in a formatted table with taste score breakdown."""
    if recs.empty:
        print("No recommendations found.")
        return
    
    print(f"\n{title}")
    print("-" * 80)
    
    for _, row in recs.iterrows():
        print(f"\n{int(row['rank'])}. {row['name']}")
        if 'vicinity' in row and pd.notna(row['vicinity']):
            print(f"   📍 {row['vicinity']}")
        if 'cuisine_primary' in row and pd.notna(row['cuisine_primary']):
            print(f"   🍽️  {row['cuisine_primary'].title()}")
        if 'distance_km' in row:
            print(f"   📏 {row['distance_km']:.2f} km away")
        if 'rating' in row and pd.notna(row['rating']):
            reviews = int(row.get('user_ratings_total', 0))
            print(f"   ⭐ {row['rating']:.1f} ({reviews:,} reviews)")
        
        # Show score breakdown
        taste = row.get('taste_score', 0) * 100
        proximity = row.get('proximity_score', 0) * 100
        quality = row.get('quality_score', 0) * 100
        final = row.get('final_score', 0) * 100
        
        print(f"   📊 Taste: {taste:.0f}% | Proximity: {proximity:.0f}% | Quality: {quality:.0f}% → Score: {final:.0f}%")
        
        # Show taste score breakdown
        breakdown = get_taste_breakdown(
            location_id=int(row['location_id']),
            user_id=user_id,
            user_tags=user_tags,
            location_tags=location_tags,
            top_n=3
        )
        
        if breakdown:
            print(f"   🎯 Taste matches:")
            for tag_text, user_score, loc_score, contribution in breakdown:
                print(f"      • {tag_text}: You({user_score:.0f}) × Place({loc_score:.0f}) = {contribution*100:.1f}%")


def demo_supabase_user(user_id: str, center_lat: float, center_lon: float, radius_km: float = 2.0):
    """
    Demo: Get recommendations for a real Supabase user.
    
    Args:
        user_id: UUID of user in Supabase
        center_lat: Latitude of search center
        center_lon: Longitude of search center
        radius_km: Search radius in kilometers
    """
    print_section(f"PROXIMAL RECOMMENDATIONS - SUPABASE USER")
    
    # Setup
    DATA_DIR = Path("data/raw")
    CITY_NAME = "london"
    OUTPUT_DIR = Path("output/supabase_demo")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    paths = PipelinePaths(data_dir=DATA_DIR, city_name=CITY_NAME, output_dir=OUTPUT_DIR)
    review_cfg = ReviewTagConfig(min_unique_authors=2, min_mentions=3)
    config = PipelineConfig(paths=paths, review_tagging=review_cfg, synthetic_users=False)
    
    # Load data
    print("Loading data from Supabase and local files...")
    print()
    
    # Load tags
    tags_df = get_tags_dataframe()
    print(f"✓ Loaded {len(tags_df)} tags")
    
    # Load locations and build location tags
    locations = load_locations(paths)
    place_lookup = locations.set_index("google_place_id")["location_id"].to_dict()
    reviews = load_reviews(paths, place_lookup)
    location_tags = build_location_tags(locations, reviews, config.review_tagging)
    print(f"✓ Loaded {len(locations):,} locations")
    print(f"✓ Built {len(location_tags):,} location tags")
    
    # Load user tag affinities from Supabase
    print()
    print("Loading user preferences from Supabase...")
    supabase_affinities = load_user_tag_affinities_from_supabase(user_id=user_id)
    
    if supabase_affinities.empty:
        print(f"❌ No tag affinities found for user {user_id}")
        print("   Make sure this user exists in the user_tag_affinities table")
        return
    
    user_tags = convert_supabase_affinities_to_profile_format(supabase_affinities, tags_df)
    print(f"✓ Loaded {len(user_tags)} tag preferences for user")
    
    # Display user profile
    print()
    display_user_profile(user_tags, user_id, top_n=10)
    
    # Build recommendations
    print()
    print(f"🔍 Finding recommendations near ({center_lat:.4f}, {center_lon:.4f})")
    print(f"   Radius: {radius_km} km")
    print()
    
    config_obj = ProximalConfig(
        radius_km=radius_km,
        taste_weight=0.2,
        proximity_weight=0.6,
        quality_weight=0.2,
        max_results=10
    )
    
    recommendations = build_proximal_recommendations(
        user_id=user_id,
        center_lat=center_lat,
        center_lon=center_lon,
        locations=locations,
        location_tags=location_tags,
        user_tags=user_tags,
        config=config_obj
    )
    
    if recommendations.empty:
        print("No recommendations found. Try increasing the radius.")
        return
    
    display_recommendations(
        recommendations, 
        user_id=user_id,
        user_tags=user_tags,
        location_tags=location_tags,
        title=f"Top {len(recommendations)} Recommendations"
    )
    
    # Summary stats
    print()
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    print(f"Total recommendations: {len(recommendations)}")
    print(f"Average distance: {recommendations['distance_km'].mean():.2f} km")
    print(f"Average score: {recommendations['final_score'].mean()*100:.1f}%")
    print(f"Score weights: Taste={config_obj.taste_weight*100:.0f}% | Proximity={config_obj.proximity_weight*100:.0f}% | Quality={config_obj.quality_weight*100:.0f}%")


def main():
    """Main execution."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Demo proximal recommendations for a Supabase user")
    parser.add_argument("--user-id", type=str, default="7fc1dc17-b217-4eac-8f64-5dd410592571",
                        help="User UUID from Supabase")
    parser.add_argument("--lat", type=float, default=51.5130,
                        help="Search center latitude (default: King's Cross, London)")
    parser.add_argument("--lon", type=float, default=-0.1240,
                        help="Search center longitude (default: King's Cross, London)")
    parser.add_argument("--radius", type=float, default=2.0,
                        help="Search radius in kilometers (default: 2.0)")
    
    args = parser.parse_args()
    
    demo_supabase_user(
        user_id=args.user_id,
        center_lat=args.lat,
        center_lon=args.lon,
        radius_km=args.radius
    )


if __name__ == "__main__":
    main()
