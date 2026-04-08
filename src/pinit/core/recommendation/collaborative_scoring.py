"""
Collaborative filtering score: item-based co-occurrence.

Users who save overlapping sets of restaurants share taste beyond what the
25 fixed vibe tags capture. This module computes a score for each candidate
location based on how similar it is to the user's previously saved/visited set,
using a precomputed location_similarities table.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Action strength weights for the user's own interactions
USER_ACTION_STRENGTH: Dict[str, float] = {
    "check_in": 1.0,
    "save": 0.7,
    "rating": 0.6,
}

# Recency half-life for user's own actions (90 days)
USER_RECENCY_HALFLIFE_DAYS = 90.0

# Minimum saves before collaborative score is computed
MIN_SAVES_FOR_COLLAB = 3


def _parse_timestamp(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    try:
        ts_str = str(ts).replace("Z", "+00:00")
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None


def compute_collaborative_scores(
    user_id: str,
    candidate_location_ids: List[int],
    supabase: Any,
) -> Dict[int, float]:
    """
    Compute collaborative filtering scores for candidate locations.

    Uses the precomputed location_similarities table to find how similar
    each candidate is to the user's previously saved/visited locations.

    Args:
        user_id: The requesting user's ID.
        candidate_location_ids: Location IDs to score.
        supabase: SupabaseService instance.

    Returns:
        Dict mapping location_id -> collaborative_score (0-1).
    """
    if not candidate_location_ids:
        return {}

    # 1. Fetch user's positive actions (their "saved set")
    user_actions = supabase.get_user_positive_actions(user_id, limit=100)
    if len(user_actions) < MIN_SAVES_FOR_COLLAB:
        logger.debug(
            "User %s has %d actions (< %d minimum), returning 0.0 collaborative scores",
            user_id, len(user_actions), MIN_SAVES_FOR_COLLAB,
        )
        return {loc_id: 0.0 for loc_id in candidate_location_ids}

    # Build saved set with recency and action strength weights
    now = datetime.now(timezone.utc)
    saved_weights: Dict[int, float] = {}
    for action_row in user_actions:
        loc_id = action_row["location_id"]
        action_type = action_row.get("action", "save")
        strength = USER_ACTION_STRENGTH.get(action_type, 0.5)

        ts = _parse_timestamp(action_row.get("created_at"))
        if ts:
            days_ago = max((now - ts).total_seconds() / 86400, 0)
            recency = math.exp(-days_ago / USER_RECENCY_HALFLIFE_DAYS)
        else:
            recency = 0.5

        weight = strength * recency
        # Keep the strongest signal per location
        if loc_id not in saved_weights or weight > saved_weights[loc_id]:
            saved_weights[loc_id] = weight

    saved_location_ids = list(saved_weights.keys())

    # Exclude already-saved locations from candidates (don't recommend what they already have)
    candidate_set = set(candidate_location_ids)
    unsaved_candidates = [lid for lid in candidate_location_ids if lid not in saved_weights]

    if not unsaved_candidates:
        # All candidates are already saved -- still score them but they'll rank naturally
        unsaved_candidates = list(candidate_location_ids)

    # 2. Fetch similarities between user's saved set and candidates
    similarities = supabase.get_location_similarities(
        location_ids=unsaved_candidates,
        saved_location_ids=saved_location_ids,
    )

    if not similarities:
        return {loc_id: 0.0 for loc_id in candidate_location_ids}

    # 3. Score each candidate: weighted sum of similarities to saved set
    raw_scores: Dict[int, float] = defaultdict(float)
    for sim_row in similarities:
        saved_loc = sim_row["location_id_a"]
        candidate_loc = sim_row["location_id_b"]
        sim_score = sim_row["similarity_score"]

        # Weight similarity by how strongly/recently the user saved that location
        user_weight = saved_weights.get(saved_loc, 0.5)
        raw_scores[candidate_loc] += sim_score * user_weight

    # 4. Normalize to [0, 1]
    scores: Dict[int, float] = {}
    if raw_scores:
        max_score = max(raw_scores.values())
        for loc_id in candidate_location_ids:
            raw = raw_scores.get(loc_id, 0.0)
            scores[loc_id] = raw / max_score if max_score > 0 else 0.0
    else:
        scores = {loc_id: 0.0 for loc_id in candidate_location_ids}

    return scores
