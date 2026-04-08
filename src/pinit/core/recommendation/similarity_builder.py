"""
Batch job: build the location_similarities table from user_location_actions.

Computes item-based collaborative filtering similarities using cosine similarity
on the user-save-vector space:

    similarity(A, B) = |users_who_saved_both| / sqrt(|savers_A| * |savers_B|)

Run periodically (daily/weekly) via CLI:
    python -m pinit.core.recommendation.similarity_builder
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Set, Tuple

logger = logging.getLogger(__name__)

# Only compute similarity when at least this many users saved both locations
MIN_CO_SAVES = 2

# Store top-K most similar locations per location
TOP_K_SIMILAR = 20


def build_similarities(supabase: Any) -> int:
    """
    Build the location similarity matrix from all positive user actions.

    Args:
        supabase: SupabaseService instance.

    Returns:
        Number of similarity rows upserted.
    """
    logger.info("Starting similarity build...")

    # 1. Fetch all positive actions
    actions = supabase.get_all_positive_actions(limit=50000)
    if not actions:
        logger.warning("No positive actions found, skipping similarity build")
        return 0

    logger.info("Loaded %d positive actions", len(actions))

    # 2. Build location -> set of users mapping
    location_users: Dict[int, Set[str]] = defaultdict(set)
    for action in actions:
        location_users[action["location_id"]].add(action["user_id"])

    location_ids = list(location_users.keys())
    logger.info("Found %d locations with positive actions", len(location_ids))

    # 3. Compute pairwise cosine similarity
    # For efficiency, build inverted index: user -> set of locations
    user_locations: Dict[str, Set[int]] = defaultdict(set)
    for loc_id, users in location_users.items():
        for user_id in users:
            user_locations[user_id].add(loc_id)

    # Count co-occurrences via inverted index (much faster than O(L^2) for sparse data)
    co_saves: Dict[Tuple[int, int], int] = defaultdict(int)
    for user_id, locs in user_locations.items():
        loc_list = sorted(locs)
        for i in range(len(loc_list)):
            for j in range(i + 1, len(loc_list)):
                pair = (loc_list[i], loc_list[j])
                co_saves[pair] += 1

    logger.info("Found %d location pairs with co-saves", len(co_saves))

    # 4. Compute cosine similarity and keep top-K per location
    # similarity(A, B) = co_saves(A,B) / sqrt(|savers_A| * |savers_B|)
    sim_by_location: Dict[int, List[Tuple[int, float, int]]] = defaultdict(list)

    for (loc_a, loc_b), co_count in co_saves.items():
        if co_count < MIN_CO_SAVES:
            continue

        savers_a = len(location_users[loc_a])
        savers_b = len(location_users[loc_b])
        denom = math.sqrt(savers_a * savers_b)

        if denom == 0:
            continue

        similarity = co_count / denom

        # Store both directions
        sim_by_location[loc_a].append((loc_b, similarity, co_count))
        sim_by_location[loc_b].append((loc_a, similarity, co_count))

    # 5. Keep top-K per location and prepare rows for upsert
    rows: List[Dict[str, Any]] = []
    seen_pairs: Set[Tuple[int, int]] = set()

    for loc_id, sims in sim_by_location.items():
        # Sort by similarity descending, keep top-K
        sims.sort(key=lambda x: x[1], reverse=True)
        for other_id, sim_score, co_count in sims[:TOP_K_SIMILAR]:
            # Store both directions but avoid duplicate pairs
            pair_key = (min(loc_id, other_id), max(loc_id, other_id))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            # Store as A -> B (A is the saved location, B is the candidate)
            rows.append({
                "location_id_a": loc_id,
                "location_id_b": other_id,
                "similarity_score": round(sim_score, 6),
                "co_save_count": co_count,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            rows.append({
                "location_id_a": other_id,
                "location_id_b": loc_id,
                "similarity_score": round(sim_score, 6),
                "co_save_count": co_count,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

    logger.info("Upserting %d similarity rows...", len(rows))

    # 6. Batch upsert in chunks
    total_upserted = 0
    chunk_size = 500
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i:i + chunk_size]
        count = supabase.upsert_location_similarities(chunk)
        total_upserted += count

    logger.info("Similarity build complete: %d rows upserted", total_upserted)
    return total_upserted


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from pinit.integrations.supabase import get_supabase_service
    supabase = get_supabase_service()
    count = build_similarities(supabase)
    print(f"Done. Upserted {count} similarity rows.")
    sys.exit(0)
