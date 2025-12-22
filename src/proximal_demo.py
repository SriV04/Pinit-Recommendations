"""
Demo script for proximal (location-based) recommendations.
Shows how to get personalized recommendations within a geographic radius.
"""

from pathlib import Path
import pandas as pd

from config import PipelineConfig, PipelinePaths, ReviewTagConfig
from recommendation.tag_taxonomy import get_tags_dataframe
from recommendation.static_tagging import load_locations, load_reviews, build_location_tags
from recommendation.user_profiles import ensure_user_actions, build_user_tag_affinities
from recommendation.proximal_recommendation import (
    build_proximal_recommendations,
    build_batch_proximal_recommendations,
    ProximalConfig,
    get_location_coordinates
)


def print_section(title: str):
    """Print a formatted section header."""
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}\n")


def display_recommendations(recs: pd.DataFrame, title: str = "Recommendations"):
    """Display recommendations in a formatted table."""
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


def demo_single_user():
    """Demo: Recommendations for a single user at a specific location."""
    print_section("PROXIMAL RECOMMENDATION DEMO - SINGLE USER")
    
    # Setup
    DATA_DIR = Path("data/raw")
    CITY_NAME = "london"
    OUTPUT_DIR = Path("output/proximal_demo")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    paths = PipelinePaths(data_dir=DATA_DIR, city_name=CITY_NAME, output_dir=OUTPUT_DIR)
    review_cfg = ReviewTagConfig(min_unique_authors=2, min_mentions=3)
    config = PipelineConfig(paths=paths, review_tagging=review_cfg, synthetic_users=True)
    
    # Load data
    print("Loading data...")
    tags_df = get_tags_dataframe()
    locations = load_locations(paths)
    place_lookup = locations.set_index("google_place_id")["location_id"].to_dict()
    reviews = load_reviews(paths, place_lookup)
    location_tags = build_location_tags(locations, reviews, config.review_tagging)
    
    print(f"✓ Loaded {len(locations):,} locations")
    print(f"✓ Loaded {len(tags_df)} tags")
    print(f"✓ Created {len(location_tags):,} location-tag associations")
    
    # Build user profiles
    user_actions, synthetic = ensure_user_actions(paths, locations, location_tags, allow_synthetic=True)
    user_tags, user_history = build_user_tag_affinities(user_actions, location_tags, locations)
    
    print(f"✓ Generated profiles for {user_tags['user_id'].nunique()} users")
    
    # Pick a test location (e.g., central London)
    # Coordinates for Covent Garden, London
    CENTER_LAT = 51.5130
    CENTER_LON = -0.1240
    
    print(f"\n📍 Search center: Covent Garden, London ({CENTER_LAT}, {CENTER_LON})")
    
    # Pick first user
    test_user = user_tags['user_id'].iloc[0]
    print(f"👤 User: {test_user}")
    
    # Show user's taste profile
    user_profile = user_tags[user_tags['user_id'] == test_user].head(5)
    print(f"\n🎯 User's top taste preferences:")
    tag_names = tags_df.set_index('tag_id')['text'].to_dict()
    for _, tag in user_profile.iterrows():
        tag_name = tag_names.get(tag['tag_id'], tag.get('tag_text', 'Unknown'))
        print(f"   • {tag_name}: {tag['score']:.0f}/100")
    
    # Generate recommendations with different radii
    radii = [1.0, 2.0, 5.0]
    
    for radius in radii:
        print_section(f"RECOMMENDATIONS WITHIN {radius}KM")
        
        proximal_config = ProximalConfig(
            radius_km=radius,
            min_results=5,
            max_results=10,
            taste_weight=0.6,
            proximity_weight=0.2,
            quality_weight=0.2
        )
        
        recs = build_proximal_recommendations(
            test_user,
            CENTER_LAT,
            CENTER_LON,
            locations,
            user_tags,
            location_tags,
            proximal_config
        )
        
        display_recommendations(recs, f"Top {len(recs)} recommendations within {radius}km")
        
        if not recs.empty:
            # Save to CSV
            output_file = OUTPUT_DIR / f"recs_{test_user}_radius_{radius}km.csv"
            recs.to_csv(output_file, index=False)
            print(f"\n✓ Saved to: {output_file}")


def demo_multiple_users():
    """Demo: Compare recommendations for different users at same location."""
    print_section("PROXIMAL RECOMMENDATION DEMO - MULTIPLE USERS")
    
    # Setup
    DATA_DIR = Path("data/raw")
    CITY_NAME = "london"
    OUTPUT_DIR = Path("output/proximal_demo")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    paths = PipelinePaths(data_dir=DATA_DIR, city_name=CITY_NAME, output_dir=OUTPUT_DIR)
    review_cfg = ReviewTagConfig(min_unique_authors=2, min_mentions=3)
    config = PipelineConfig(paths=paths, review_tagging=review_cfg, synthetic_users=True)
    
    # Load data
    print("Loading data...")
    tags_df = get_tags_dataframe()
    locations = load_locations(paths)
    place_lookup = locations.set_index("google_place_id")["location_id"].to_dict()
    reviews = load_reviews(paths, place_lookup)
    location_tags = build_location_tags(locations, reviews, config.review_tagging)
    user_actions, synthetic = ensure_user_actions(paths, locations, location_tags, allow_synthetic=True)
    user_tags, user_history = build_user_tag_affinities(user_actions, location_tags, locations)
    
    # Shoreditch, London (trendy area)
    CENTER_LAT = 51.5254
    CENTER_LON = -0.0854
    
    print(f"\n📍 Search center: Shoreditch, London ({CENTER_LAT}, {CENTER_LON})")
    
    # Get all users
    all_users = user_tags['user_id'].unique()[:3]  # Take first 3 users
    
    proximal_config = ProximalConfig(
        radius_km=2.0,
        min_results=5,
        max_results=8,
        taste_weight=0.6,
        proximity_weight=0.2,
        quality_weight=0.2
    )
    
    tag_names = tags_df.set_index('tag_id')['text'].to_dict()
    
    for user_id in all_users:
        print(f"\n{'='*80}")
        print(f"👤 USER: {user_id}")
        print(f"{'='*80}")
        
        # Show user profile
        user_profile = user_tags[user_tags['user_id'] == user_id].head(3)
        print(f"\n🎯 Top preferences:")
        for _, tag in user_profile.iterrows():
            tag_name = tag_names.get(tag['tag_id'], tag.get('tag_text', 'Unknown'))
            print(f"   • {tag_name}: {tag['score']:.0f}/100")
        
        # Generate recommendations
        recs = build_proximal_recommendations(
            user_id,
            CENTER_LAT,
            CENTER_LON,
            locations,
            user_tags,
            location_tags,
            proximal_config
        )
        
        display_recommendations(recs.head(5), f"\nTop 5 recommendations")
    
    print_section("BATCH RECOMMENDATIONS")
    
    # Generate for all users at once
    batch_recs = build_batch_proximal_recommendations(
        all_users.tolist(),
        CENTER_LAT,
        CENTER_LON,
        locations,
        user_tags,
        location_tags,
        proximal_config
    )
    
    print(f"Generated {len(batch_recs)} total recommendations across {len(all_users)} users")
    
    # Save batch results
    output_file = OUTPUT_DIR / "batch_proximal_recommendations.csv"
    batch_recs.to_csv(output_file, index=False)
    print(f"✓ Saved to: {output_file}")


def demo_location_to_location():
    """Demo: Recommendations near a specific restaurant."""
    print_section("PROXIMAL RECOMMENDATION DEMO - NEAR A LOCATION")
    
    # Setup
    DATA_DIR = Path("data/raw")
    CITY_NAME = "london"
    OUTPUT_DIR = Path("output/proximal_demo")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    paths = PipelinePaths(data_dir=DATA_DIR, city_name=CITY_NAME, output_dir=OUTPUT_DIR)
    review_cfg = ReviewTagConfig(min_unique_authors=2, min_mentions=3)
    config = PipelineConfig(paths=paths, review_tagging=review_cfg, synthetic_users=True)
    
    # Load data
    print("Loading data...")
    tags_df = get_tags_dataframe()
    locations = load_locations(paths)
    place_lookup = locations.set_index("google_place_id")["location_id"].to_dict()
    reviews = load_reviews(paths, place_lookup)
    location_tags = build_location_tags(locations, reviews, config.review_tagging)
    user_actions, synthetic = ensure_user_actions(paths, locations, location_tags, allow_synthetic=True)
    user_tags, user_history = build_user_tag_affinities(user_actions, location_tags, locations)
    
    # Pick a reference location
    ref_location = locations.iloc[0]
    ref_coords = get_location_coordinates(ref_location['location_id'], locations)
    
    if ref_coords is None:
        print("❌ Reference location has no coordinates")
        return
    
    ref_lat, ref_lon = ref_coords
    
    print(f"\n📍 Reference location: {ref_location['name']}")
    print(f"   Address: {ref_location.get('vicinity', 'N/A')}")
    print(f"   Coordinates: ({ref_lat:.4f}, {ref_lon:.4f})")
    
    # Pick a user
    test_user = user_tags['user_id'].iloc[0]
    
    proximal_config = ProximalConfig(
        radius_km=1.5,  # 1.5km radius
        min_results=5,
        max_results=10,
        taste_weight=0.7,  # Higher weight on taste
        proximity_weight=0.15,
        quality_weight=0.15
    )
    
    print(f"\n👤 Finding recommendations for: {test_user}")
    print(f"🔍 Within {proximal_config.radius_km}km of {ref_location['name']}")
    
    recs = build_proximal_recommendations(
        test_user,
        ref_lat,
        ref_lon,
        locations,
        user_tags,
        location_tags,
        proximal_config
    )
    
    # Exclude the reference location itself
    recs = recs[recs['location_id'] != ref_location['location_id']]
    
    display_recommendations(recs.head(10), f"\nSimilar places nearby")
    
    # Save
    output_file = OUTPUT_DIR / f"near_{ref_location['location_id']}.csv"
    recs.to_csv(output_file, index=False)
    print(f"\n✓ Saved to: {output_file}")


def main():
    """Run all demos."""
    import sys
    
    if len(sys.argv) > 1:
        demo_type = sys.argv[1]
        if demo_type == "single":
            demo_single_user()
        elif demo_type == "multiple":
            demo_multiple_users()
        elif demo_type == "location":
            demo_location_to_location()
        else:
            print(f"Unknown demo type: {demo_type}")
            print("Usage: python proximal_demo.py [single|multiple|location]")
    else:
        # Run all demos
        demo_single_user()
        demo_multiple_users()
        demo_location_to_location()
    
    print_section("DEMO COMPLETE")
    print("All outputs saved to: output/proximal_demo/")


if __name__ == "__main__":
    main()
