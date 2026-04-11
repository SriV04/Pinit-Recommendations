"""
Social scoring: compute recommendation scores based on friends' interactions.

Leverages the user_friends social graph and user_location_actions to surface
places that friends have saved, checked into, or rated highly.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Action weights: how strongly each action type signals recommendation quality.
# Values must match the action_type enum in the DB:
#   save, like, shared_video, dislike, bubble_save, been_to
ACTION_WEIGHTS: Dict[str, float] = {
    "been_to": 1.0,        # strongest signal — friend actually visited
    "shared_video": 0.9,   # posted it to their feed
    "save": 0.7,
    "bubble_save": 0.7,
    "like": 0.5,
    "dislike": -0.5,
}

# Recency half-life in days (60-day exponential decay)
RECENCY_HALFLIFE_DAYS = 60.0

# Default influence weight when friend has no influence score
DEFAULT_INFLUENCE = 0.5


def _recency_decay(days_ago: float) -> float:
    """Exponential decay: exp(-days / halflife)."""
    return math.exp(-days_ago / RECENCY_HALFLIFE_DAYS)


def _action_weight(action: str) -> float:
    """Get the weight for an action type."""
    return ACTION_WEIGHTS.get(action, 0.0)


def _parse_timestamp(ts: Any) -> Optional[datetime]:
    """Parse a timestamp string or datetime into a timezone-aware datetime."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    try:
        # Handle ISO format strings from Supabase
        ts_str = str(ts).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(ts_str)
        # Supabase serialises `timestamp without time zone` columns with no
        # offset. Treat those as UTC so arithmetic against `datetime.now(utc)`
        # doesn't raise `can't subtract offset-naive and offset-aware datetimes`.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (ValueError, TypeError):
        return None


def compute_social_scores(
    user_id: str,
    candidate_location_ids: List[int],
    supabase: Any,
) -> Tuple[Dict[int, float], Dict[int, list]]:
    """
    Compute social scores and friend attribution for candidate locations.

    Args:
        user_id: The requesting user's ID.
        candidate_location_ids: Location IDs to score.
        supabase: SupabaseService instance.

    Returns:
        Tuple of:
        - Dict mapping location_id -> social_score (0-1)
        - Dict mapping location_id -> list of FriendSave-compatible dicts
    """
    if not candidate_location_ids:
        return {}, {}

    # 1. Fetch user's friends
    friends = supabase.get_user_friends(user_id)
    if not friends:
        return {loc_id: 0.0 for loc_id in candidate_location_ids}, {}

    friend_ids = [f["friend_id"] for f in friends]
    influence_map = {
        f["friend_id"]: (f["influence"] / 100.0 if f.get("influence") else DEFAULT_INFLUENCE)
        for f in friends
    }

    # 2. Fetch friends' actions on candidate locations
    actions = supabase.get_friends_location_actions(friend_ids, candidate_location_ids)
    if not actions:
        return {loc_id: 0.0 for loc_id in candidate_location_ids}, {}

    # 3. Fetch friend profiles for attribution display
    acting_friend_ids = list({a["user_id"] for a in actions})
    profiles = supabase.get_friend_profiles(acting_friend_ids)

    # 4. Compute scores and build attribution
    now = datetime.now(timezone.utc)
    raw_scores: Dict[int, float] = defaultdict(float)
    attributions: Dict[int, list] = defaultdict(list)

    for action_row in actions:
        loc_id = action_row["location_id"]
        friend_id = action_row["user_id"]
        action_type = action_row.get("action", "save")

        # Compute weight
        weight = _action_weight(action_type)
        influence = influence_map.get(friend_id, DEFAULT_INFLUENCE)

        # Recency
        ts = _parse_timestamp(action_row.get("created_at"))
        if ts:
            days_ago = max((now - ts).total_seconds() / 86400, 0)
            recency = _recency_decay(days_ago)
        else:
            recency = 0.5  # unknown timestamp gets half weight

        raw_scores[loc_id] += weight * recency * influence

        # Build attribution entry
        profile = profiles.get(friend_id, {})
        attributions[loc_id].append({
            "friend_id": friend_id,
            "friend_name": profile.get("name", ""),
            "friend_username": profile.get("username"),
            "friend_profile_image_url": profile.get("profile_image_url"),
            "action_type": action_type,
            "rating": None,
            "timestamp": str(action_row.get("created_at", "")),
        })

    # 5. Normalize to [0, 1] via min-max
    scores: Dict[int, float] = {}
    if raw_scores:
        max_score = max(raw_scores.values())
        min_score = min(raw_scores.values())
        score_range = max_score - min_score

        for loc_id in candidate_location_ids:
            raw = raw_scores.get(loc_id, 0.0)
            if score_range > 0:
                scores[loc_id] = max(0.0, (raw - min(0.0, min_score)) / (max_score - min(0.0, min_score)))
            elif max_score > 0:
                scores[loc_id] = 1.0 if raw > 0 else 0.0
            else:
                scores[loc_id] = 0.0
    else:
        scores = {loc_id: 0.0 for loc_id in candidate_location_ids}

    # Sort attributions by recency (most recent first)
    for loc_id in attributions:
        attributions[loc_id].sort(key=lambda x: x["timestamp"], reverse=True)

    return scores, dict(attributions)
