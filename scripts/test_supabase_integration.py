"""
Quick test script to verify Supabase user tag affinities integration.
"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pinit.core.recommendation.user_profiles import (
    load_user_tag_affinities_from_supabase,
    convert_supabase_affinities_to_profile_format
)
from pinit.core.recommendation.tag_taxonomy import get_tags_dataframe


def main():
    print("=" * 60)
    print("User Tag Affinities - Supabase Integration Test")
    print("=" * 60)
    
    # Test 1: Load tags
    print("\n1. Loading tags from Supabase...")
    try:
        tags_df = get_tags_dataframe()
        print(f"   ✓ Loaded {len(tags_df)} tags")
    except Exception as e:
        print(f"   ✗ Error loading tags: {e}")
        return
    
    # Test 2: Load user tag affinities
    print("\n2. Loading user tag affinities from Supabase...")
    try:
        affinities = load_user_tag_affinities_from_supabase()
        if affinities.empty:
            print("   ⚠ No user tag affinities found in Supabase")
            print("   Tip: Run 'python scripts/sync_user_affinities.py --synthetic' to populate data")
        else:
            print(f"   ✓ Loaded {len(affinities)} affinity records")
            print(f"   ✓ {affinities['user_id'].nunique()} unique users")
            print(f"   ✓ {affinities['tag_id'].nunique()} unique tags")
            
            # Show sample
            print("\n   Sample records:")
            print(affinities.head(3).to_string(index=False))
    except Exception as e:
        print(f"   ✗ Error loading affinities: {e}")
        return
    
    # Test 3: Format conversion
    if not affinities.empty:
        print("\n3. Converting to recommendation system format...")
        try:
            profile_format = convert_supabase_affinities_to_profile_format(
                affinities, tags_df
            )
            print(f"   ✓ Converted {len(profile_format)} records")
            print(f"   ✓ Score range: {profile_format['score'].min():.1f} - {profile_format['score'].max():.1f}")
            
            # Show sample with tag text
            print("\n   Sample converted records:")
            print(profile_format.head(3)[['user_id', 'tag_text', 'score']].to_string(index=False))
        except Exception as e:
            print(f"   ✗ Error converting format: {e}")
            return
    
    # Test 4: Load specific user
    if not affinities.empty:
        sample_user = affinities['user_id'].iloc[0]
        print(f"\n4. Loading affinities for specific user: {sample_user}")
        try:
            user_affinities = load_user_tag_affinities_from_supabase(user_id=sample_user)
            print(f"   ✓ Loaded {len(user_affinities)} affinities for this user")
        except Exception as e:
            print(f"   ✗ Error loading user affinities: {e}")
    
    # Test 5: Filter by threshold
    if not affinities.empty:
        print("\n5. Testing affinity threshold filter...")
        try:
            high_affinities = load_user_tag_affinities_from_supabase(min_affinity=0.5)
            print(f"   ✓ Found {len(high_affinities)} high-affinity records (>= 0.5)")
            pct = (len(high_affinities) / len(affinities)) * 100
            print(f"   ✓ That's {pct:.1f}% of all affinities")
        except Exception as e:
            print(f"   ✗ Error filtering affinities: {e}")
    
    print("\n" + "=" * 60)
    print("Test Complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
