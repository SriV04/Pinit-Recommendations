"""
Main demonstration script for the Pinit recommendation pipeline.
Showcases the pipeline steps with visualizations and analysis.
"""

import json
from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

from config import PipelineConfig, PipelinePaths, ReviewTagConfig
from recommendation.tag_taxonomy import get_tags_dataframe, get_tags_by_category
from recommendation.static_tagging import load_locations, load_reviews, build_location_tags
from recommendation.user_profiles import ensure_user_actions, build_user_tag_affinities
from recommendation.recommendation import build_recommendations

# Set up plotting style
sns.set_style("whitegrid")
plt.rcParams['figure.figsize'] = (12, 6)


def print_section(title: str):
    """Print a formatted section header."""
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}\n")


def visualize_tags(tags_df: pd.DataFrame, output_dir: Path):
    """Create visualizations for tag taxonomy."""
    print_section("TAG TAXONOMY ANALYSIS")
    
    # Tag counts by type
    tag_counts = tags_df['tag_type'].value_counts()
    print(f"Total tags: {len(tags_df)}")
    print(f"\nTags by type:")
    for tag_type, count in tag_counts.items():
        print(f"  {tag_type:15s}: {count:3d} tags")
    
    # Plot tag distribution
    fig, ax = plt.subplots(figsize=(10, 6))
    tag_counts.plot(kind='bar', ax=ax, color='steelblue')
    ax.set_title('Tag Distribution by Type', fontsize=14, fontweight='bold')
    ax.set_xlabel('Tag Type', fontsize=12)
    ax.set_ylabel('Number of Tags', fontsize=12)
    ax.tick_params(axis='x', rotation=45)
    plt.tight_layout()
    plt.savefig(output_dir / 'tag_distribution.png', dpi=300)
    print(f"\n✓ Saved: {output_dir / 'tag_distribution.png'}")
    plt.close()


def visualize_locations(locations: pd.DataFrame, output_dir: Path):
    """Create visualizations for location data."""
    print_section("LOCATION INVENTORY ANALYSIS")
    
    print(f"Total locations: {len(locations):,}")
    print(f"Average rating: {locations['rating'].mean():.2f}")
    print(f"Total reviews: {locations['user_ratings_total'].sum():,.0f}")
    
    # Cuisine distribution
    top_cuisines = locations['cuisine_primary'].value_counts().head(10)
    print(f"\nTop 10 cuisines:")
    for cuisine, count in top_cuisines.items():
        print(f"  {cuisine:20s}: {count:3d} locations")
    
    # Create visualizations
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    
    # 1. Cuisine distribution
    top_cuisines.plot(kind='barh', ax=axes[0, 0], color='coral')
    axes[0, 0].set_title('Top 10 Cuisines', fontsize=12, fontweight='bold')
    axes[0, 0].set_xlabel('Number of Locations')
    
    # 2. Rating distribution
    axes[0, 1].hist(locations['rating'].dropna(), bins=20, color='lightblue', edgecolor='black')
    axes[0, 1].set_title('Rating Distribution', fontsize=12, fontweight='bold')
    axes[0, 1].set_xlabel('Rating')
    axes[0, 1].set_ylabel('Frequency')
    axes[0, 1].axvline(locations['rating'].mean(), color='red', linestyle='--', label='Mean')
    axes[0, 1].legend()
    
    # 3. Price level distribution
    price_counts = locations['price_bucket'].value_counts()
    axes[1, 0].bar(price_counts.index, price_counts.values, color='gold', edgecolor='black')
    axes[1, 0].set_title('Price Level Distribution', fontsize=12, fontweight='bold')
    axes[1, 0].set_xlabel('Price Bucket')
    axes[1, 0].set_ylabel('Count')
    
    # 4. Review count vs rating scatter
    sample = locations.sample(min(500, len(locations)))
    axes[1, 1].scatter(sample['user_ratings_total'], sample['rating'], alpha=0.5, color='purple')
    axes[1, 1].set_title('Reviews vs Rating', fontsize=12, fontweight='bold')
    axes[1, 1].set_xlabel('Number of Reviews')
    axes[1, 1].set_ylabel('Rating')
    axes[1, 1].set_xscale('log')
    
    plt.tight_layout()
    plt.savefig(output_dir / 'location_analysis.png', dpi=300)
    print(f"\n✓ Saved: {output_dir / 'location_analysis.png'}")
    plt.close()


def visualize_location_tags(location_tags: pd.DataFrame, tags_df: pd.DataFrame, output_dir: Path):
    """Create visualizations for location tagging."""
    print_section("LOCATION TAGGING ANALYSIS")
    
    print(f"Total location-tag pairs: {len(location_tags):,}")
    print(f"Unique locations tagged: {location_tags['location_id'].nunique():,}")
    print(f"Unique tags used: {location_tags['tag_id'].nunique():,}")
    
    # Tags per location
    tags_per_location = location_tags.groupby('location_id').size()
    print(f"\nTags per location:")
    print(f"  Mean: {tags_per_location.mean():.1f}")
    print(f"  Median: {tags_per_location.median():.0f}")
    print(f"  Max: {tags_per_location.max():.0f}")
    
    # Most common tags
    tag_usage = location_tags['tag_id'].value_counts().head(15)
    tag_names = tags_df.set_index('tag_id')['text'].to_dict()
    
    print(f"\nMost frequently used tags:")
    for tag_id, count in tag_usage.items():
        tag_name = tag_names.get(tag_id, 'Unknown')
        print(f"  {tag_name:25s}: {count:4d} locations")
    
    # Visualizations
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    
    # 1. Tags per location histogram
    axes[0].hist(tags_per_location, bins=30, color='teal', edgecolor='black')
    axes[0].set_title('Distribution of Tags per Location', fontsize=12, fontweight='bold')
    axes[0].set_xlabel('Number of Tags')
    axes[0].set_ylabel('Number of Locations')
    axes[0].axvline(tags_per_location.mean(), color='red', linestyle='--', label='Mean')
    axes[0].legend()
    
    # 2. Top tags
    top_tag_names = [tag_names.get(tid, 'Unknown') for tid in tag_usage.head(10).index]
    axes[1].barh(range(len(top_tag_names)), tag_usage.head(10).values, color='mediumseagreen')
    axes[1].set_yticks(range(len(top_tag_names)))
    axes[1].set_yticklabels(top_tag_names)
    axes[1].set_title('Top 10 Most Used Tags', fontsize=12, fontweight='bold')
    axes[1].set_xlabel('Number of Locations')
    axes[1].invert_yaxis()
    
    plt.tight_layout()
    plt.savefig(output_dir / 'location_tags_analysis.png', dpi=300)
    print(f"\n✓ Saved: {output_dir / 'location_tags_analysis.png'}")
    plt.close()


def visualize_user_profiles(user_tags: pd.DataFrame, tags_df: pd.DataFrame, output_dir: Path):
    """Create visualizations for user taste profiles."""
    print_section("USER TASTE PROFILES")
    
    n_users = user_tags['user_id'].nunique()
    print(f"Total users: {n_users}")
    print(f"Total user-tag affinities: {len(user_tags):,}")
    
    # Affinities per user
    affinities_per_user = user_tags.groupby('user_id').size()
    print(f"\nTag affinities per user:")
    print(f"  Mean: {affinities_per_user.mean():.1f}")
    print(f"  Median: {affinities_per_user.median():.0f}")
    
    # Top tags across all users
    avg_score_by_tag = user_tags.groupby('tag_id')['score'].agg(['mean', 'count'])
    top_tags = avg_score_by_tag.nlargest(10, 'mean')
    tag_names = tags_df.set_index('tag_id')['text'].to_dict()
    
    print(f"\nTop tags by average score:")
    for tag_id, row in top_tags.iterrows():
        tag_name = tag_names.get(tag_id, 'Unknown')
        print(f"  {tag_name:25s}: {row['mean']:.1f} (n={int(row['count'])})")
    
    # Visualizations
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    
    # 1. Affinities per user
    axes[0].hist(affinities_per_user, bins=20, color='orchid', edgecolor='black')
    axes[0].set_title('Tag Affinities per User', fontsize=12, fontweight='bold')
    axes[0].set_xlabel('Number of Tag Affinities')
    axes[0].set_ylabel('Number of Users')
    
    # 2. Top tags by score
    top_tag_names = [tag_names.get(tid, 'Unknown') for tid in top_tags.index]
    axes[1].barh(range(len(top_tag_names)), top_tags['mean'].values, color='salmon')
    axes[1].set_yticks(range(len(top_tag_names)))
    axes[1].set_yticklabels(top_tag_names)
    axes[1].set_title('Top 10 Tags by Average Score', fontsize=12, fontweight='bold')
    axes[1].set_xlabel('Average Score')
    axes[1].invert_yaxis()
    
    plt.tight_layout()
    plt.savefig(output_dir / 'user_profiles_analysis.png', dpi=300)
    print(f"\n✓ Saved: {output_dir / 'user_profiles_analysis.png'}")
    plt.close()


def visualize_recommendations(recs: pd.DataFrame, locations: pd.DataFrame, output_dir: Path):
    """Create visualizations for recommendations."""
    print_section("RECOMMENDATION RESULTS")
    
    n_users = recs['user_id'].nunique()
    n_locations = recs['location_id'].nunique()
    
    print(f"Recommendations generated for {n_users} users")
    print(f"Unique locations recommended: {n_locations}")
    print(f"Total recommendations: {len(recs):,}")
    
    # Recs per user
    recs_per_user = recs.groupby('user_id').size()
    print(f"\nRecommendations per user:")
    print(f"  Mean: {recs_per_user.mean():.1f}")
    print(f"  Median: {recs_per_user.median():.0f}")
    
    # Score distribution
    print(f"\nScore distribution:")
    print(f"  Mean: {recs['score'].mean():.2f}")
    print(f"  Median: {recs['score'].median():.2f}")
    print(f"  Min: {recs['score'].min():.2f}")
    print(f"  Max: {recs['score'].max():.2f}")
    
    # Most recommended locations
    top_recommended = recs['location_id'].value_counts().head(10)
    location_names = locations.set_index('location_id')['name'].to_dict()
    
    print(f"\nMost frequently recommended locations:")
    for loc_id, count in top_recommended.items():
        loc_name = location_names.get(loc_id, 'Unknown')
        print(f"  {loc_name[:40]:40s}: {count:3d} users")
    
    # Visualizations
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    
    # 1. Recommendations per user
    axes[0, 0].hist(recs_per_user, bins=20, color='skyblue', edgecolor='black')
    axes[0, 0].set_title('Recommendations per User', fontsize=12, fontweight='bold')
    axes[0, 0].set_xlabel('Number of Recommendations')
    axes[0, 0].set_ylabel('Number of Users')
    
    # 2. Score distribution
    axes[0, 1].hist(recs['score'], bins=30, color='lightgreen', edgecolor='black')
    axes[0, 1].set_title('Recommendation Score Distribution', fontsize=12, fontweight='bold')
    axes[0, 1].set_xlabel('Score')
    axes[0, 1].set_ylabel('Frequency')
    
    # 3. Top locations
    top_loc_names = [location_names.get(lid, 'Unknown')[:25] for lid in top_recommended.head(8).index]
    axes[1, 0].barh(range(len(top_loc_names)), top_recommended.head(8).values, color='orange')
    axes[1, 0].set_yticks(range(len(top_loc_names)))
    axes[1, 0].set_yticklabels(top_loc_names, fontsize=9)
    axes[1, 0].set_title('Most Recommended Locations', fontsize=12, fontweight='bold')
    axes[1, 0].set_xlabel('Number of Times Recommended')
    axes[1, 0].invert_yaxis()
    
    # 4. Rank distribution
    axes[1, 1].hist(recs['rank'], bins=30, color='plum', edgecolor='black')
    axes[1, 1].set_title('Recommendation Rank Distribution', fontsize=12, fontweight='bold')
    axes[1, 1].set_xlabel('Rank')
    axes[1, 1].set_ylabel('Frequency')
    
    plt.tight_layout()
    plt.savefig(output_dir / 'recommendations_analysis.png', dpi=300)
    print(f"\n✓ Saved: {output_dir / 'recommendations_analysis.png'}")
    plt.close()


def main():
    """Run the complete pipeline with visualizations."""
    print_section("PINIT RECOMMENDATION PIPELINE DEMO")
    
    # Configuration
    DATA_DIR = Path("data/raw")
    CITY_NAME = "london"
    OUTPUT_DIR = Path("output/pinit_demo")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    paths = PipelinePaths(data_dir=DATA_DIR, city_name=CITY_NAME, output_dir=OUTPUT_DIR)
    review_cfg = ReviewTagConfig(min_unique_authors=2, min_mentions=3)
    config = PipelineConfig(paths=paths, review_tagging=review_cfg, top_k_per_user=25, synthetic_users=True)
    
    print(f"Data directory: {DATA_DIR.resolve()}")
    print(f"Output directory: {OUTPUT_DIR.resolve()}")
    print(f"City: {CITY_NAME}")
    
    # Step 1: Load tags from Supabase
    print_section("STEP 1: LOAD TAGS FROM SUPABASE")
    tags_df = get_tags_dataframe()
    print(f"✓ Loaded {len(tags_df)} tags from Supabase")
    visualize_tags(tags_df, OUTPUT_DIR)
    
    # Step 2: Load locations
    print_section("STEP 2: LOAD LOCATION INVENTORY")
    locations = load_locations(paths)
    print(f"✓ Loaded {len(locations):,} locations")
    visualize_locations(locations, OUTPUT_DIR)
    
    # Step 3: Load reviews and build location tags
    print_section("STEP 3: BUILD LOCATION TAGS")
    place_lookup = locations.set_index("google_place_id")["location_id"].to_dict()
    reviews = load_reviews(paths, place_lookup)
    print(f"✓ Loaded {len(reviews):,} reviews")
    
    location_tags = build_location_tags(locations, reviews, config.review_tagging)
    print(f"✓ Generated {len(location_tags):,} location-tag pairs")
    visualize_location_tags(location_tags, tags_df, OUTPUT_DIR)
    
    # Step 4: Build user profiles
    print_section("STEP 4: BUILD USER TASTE PROFILES")
    user_actions, synthetic = ensure_user_actions(
        paths, locations, location_tags, allow_synthetic=config.synthetic_users
    )
    print(f"✓ Loaded {len(user_actions):,} user actions (synthetic={synthetic})")
    
    user_tags, user_history = build_user_tag_affinities(user_actions, location_tags, locations)
    print(f"✓ Computed {len(user_tags):,} user-tag affinities for {user_tags['user_id'].nunique()} users")
    visualize_user_profiles(user_tags, tags_df, OUTPUT_DIR)
    
    # Step 5: Generate recommendations
    print_section("STEP 5: GENERATE RECOMMENDATIONS")
    recommendations = build_recommendations(
        locations, user_tags, location_tags, user_history, user_actions, config
    )
    print(f"✓ Generated {len(recommendations):,} recommendations")
    visualize_recommendations(recommendations, locations, OUTPUT_DIR)
    
    # Step 6: Save outputs (no Supabase upload)
    print_section("STEP 6: SAVE OUTPUTS")
    locations.to_csv(OUTPUT_DIR / "locations.csv", index=False)
    tags_df.to_csv(OUTPUT_DIR / "tags.csv", index=False)
    location_tags.to_csv(OUTPUT_DIR / "location_tags.csv", index=False)
    user_tags.to_csv(OUTPUT_DIR / "user_tag_affinities.csv", index=False)
    user_history.to_csv(OUTPUT_DIR / "user_history.csv", index=False)
    recommendations.to_csv(OUTPUT_DIR / "user_recommendations.csv", index=False)
    
    metadata = {
        "city": CITY_NAME,
        "n_locations": int(len(locations)),
        "n_tags": int(len(tags_df)),
        "n_location_tags": int(len(location_tags)),
        "n_users": int(user_tags["user_id"].nunique()) if not user_tags.empty else 0,
        "n_recommendations": int(len(recommendations)),
        "synthetic_user_actions": synthetic,
    }
    (OUTPUT_DIR / "metadata.json").write_text(json.dumps(metadata, indent=2))
    
    print(f"✓ Saved locations.csv ({len(locations):,} rows)")
    print(f"✓ Saved tags.csv ({len(tags_df):,} rows)")
    print(f"✓ Saved location_tags.csv ({len(location_tags):,} rows)")
    print(f"✓ Saved user_tag_affinities.csv ({len(user_tags):,} rows)")
    print(f"✓ Saved user_history.csv ({len(user_history):,} rows)")
    print(f"✓ Saved user_recommendations.csv ({len(recommendations):,} rows)")
    print(f"✓ Saved metadata.json")
    
    print_section("PIPELINE COMPLETE!")
    print(f"All outputs saved to: {OUTPUT_DIR.resolve()}")
    print(f"\nGenerated visualizations:")
    print(f"  • tag_distribution.png")
    print(f"  • location_analysis.png")
    print(f"  • location_tags_analysis.png")
    print(f"  • user_profiles_analysis.png")
    print(f"  • recommendations_analysis.png")
    print(f"\n{'='*80}\n")


if __name__ == "__main__":
    main()
