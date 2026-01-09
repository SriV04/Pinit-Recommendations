"""
Diagnostic script to check location data and distance calculations.
"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pandas as pd

from pinit.config.settings import PipelinePaths
from pinit.core.recommendation.static_tagging import load_locations
from pinit.core.recommendation.proximal_recommendation import filter_by_radius


def diagnose_location_data():
    """Diagnose issues with location data and distance calculations."""
    
    print("="*80)
    print("LOCATION DATA DIAGNOSTIC")
    print("="*80)
    
    # Setup
    DATA_DIR = Path("data/raw")
    CITY_NAME = "london"
    OUTPUT_DIR = Path("output")
    
    paths = PipelinePaths(data_dir=DATA_DIR, city_name=CITY_NAME, output_dir=OUTPUT_DIR)
    
    # Load locations
    print("\n1. Loading locations...")
    locations = load_locations(paths)
    print(f"   Total locations: {len(locations):,}")
    
    # Check for coordinates
    print("\n2. Checking coordinate data...")
    has_lat = locations['lat'].notna().sum()
    has_lng = locations['lng'].notna().sum() if 'lng' in locations.columns else locations['lon'].notna().sum()
    has_both = (locations['lat'].notna() & (locations['lng'].notna() if 'lng' in locations.columns else locations['lon'].notna())).sum()
    
    print(f"   Locations with latitude: {has_lat:,} ({has_lat/len(locations)*100:.1f}%)")
    print(f"   Locations with longitude: {has_lng:,} ({has_lng/len(locations)*100:.1f}%)")
    print(f"   Locations with both: {has_both:,} ({has_both/len(locations)*100:.1f}%)")
    
    # Show sample coordinates
    print("\n3. Sample coordinates:")
    valid_coords = locations[locations['lat'].notna() & (locations['lng'].notna() if 'lng' in locations.columns else locations['lon'].notna())]
    if not valid_coords.empty:
        lon_col = 'lng' if 'lng' in valid_coords.columns else 'lon'
        for idx, row in valid_coords.head(5).iterrows():
            print(f"   {row['name'][:40]:40s} - ({row['lat']:.4f}, {row[lon_col]:.4f})")
    
    # Test filter by radius at different locations
    test_points = [
        ("King's Cross, London", 51.5130, -0.1240),
        ("Central London", 51.5074, -0.1278),
        ("Camden", 51.5390, -0.1426),
        ("Shoreditch", 51.5235, -0.0778),
    ]
    
    print("\n4. Testing filter_by_radius at different locations:")
    for name, lat, lon in test_points:
        print(f"\n   {name} ({lat:.4f}, {lon:.4f}):")
        for radius in [1.0, 2.0, 5.0, 10.0]:
            nearby = filter_by_radius(lat, lon, locations, radius)
            print(f"      {radius} km: {len(nearby)} locations")
            if len(nearby) > 0 and len(nearby) <= 3:
                for _, row in nearby.iterrows():
                    print(f"         • {row['name'][:40]:40s} - {row['distance_km']:.2f} km")
    
    # Check coordinate ranges
    print("\n5. Coordinate ranges:")
    if has_both > 0:
        lon_col = 'lng' if 'lng' in valid_coords.columns else 'lon'
        print(f"   Latitude range: {valid_coords['lat'].min():.4f} to {valid_coords['lat'].max():.4f}")
        print(f"   Longitude range: {valid_coords[lon_col].min():.4f} to {valid_coords[lon_col].max():.4f}")
        
        # Check if coordinates are reasonable for London
        london_lat_range = (51.2, 51.7)
        london_lon_range = (-0.5, 0.3)
        
        in_london = valid_coords[
            (valid_coords['lat'] >= london_lat_range[0]) &
            (valid_coords['lat'] <= london_lat_range[1]) &
            (valid_coords[lon_col] >= london_lon_range[0]) &
            (valid_coords[lon_col] <= london_lon_range[1])
        ]
        
        print(f"\n   Locations within London bounds: {len(in_london):,} ({len(in_london)/len(locations)*100:.1f}%)")
        
        if len(in_london) < len(valid_coords):
            out_of_bounds = valid_coords[~valid_coords.index.isin(in_london.index)]
            print(f"\n   Sample out-of-bounds locations:")
            for _, row in out_of_bounds.head(5).iterrows():
                print(f"      {row['name'][:40]:40s} - ({row['lat']:.4f}, {row[lon_col]:.4f})")
    
    print("\n" + "="*80)
    print("DIAGNOSTIC COMPLETE")
    print("="*80)


if __name__ == "__main__":
    diagnose_location_data()
