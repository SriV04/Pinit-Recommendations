"""
Bubble vibe vector construction.

Takes a list of users and (optionally) a bubble_id, and produces:
  1. A per-user "effective" vibe vector — their personal vibe_tag_affinity
     blended with the average vibe of locations they've added to *this*
     bubble. The blend is controlled by INTENT_WEIGHT.
  2. An activity-weighted average of those effective vectors → the group
     vibe vector used by the recommendation pipeline.

Design notes:
  * Activity weighting comes from compute_user_activity_scores in the
    bubble_recommendation module.
  * Users in the bubble who haven't added anything keep their personal
    vector unchanged (no fallback to the group's added locations).
  * Empty bubble_id (or zero-row response) → behaves identically to a
    plain "average user vectors" group flow.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# How much weight a user's bubble-added locations get versus their
# long-term personal vibe_tag_affinity. 0.0 = ignore bubble entirely,
# 1.0 = use only what they've added.
INTENT_WEIGHT: float = 0.5


def _to_float_array(vec: Any) -> Optional[np.ndarray]:
    """Cast a stored vector (list / numpy / None) to a 1-D float array."""
    if vec is None:
        return None
    try:
        arr = np.asarray(vec, dtype=float)
    except (TypeError, ValueError):
        return None
    if arr.ndim != 1 or arr.size == 0:
        return None
    return arr


def _pad_to(arr: np.ndarray, length: int) -> np.ndarray:
    """Right-pad with zeros so two vectors of different length can be combined."""
    if arr.size == length:
        return arr
    out = np.zeros(length, dtype=float)
    out[: arr.size] = arr[:length]
    return out


def build_per_user_intent_vectors(
    bubble_added_rows: List[Dict[str, Any]],
) -> Dict[str, np.ndarray]:
    """
    Group bubble_locations rows by `added_by` and compute the mean vibe
    vector of each user's contributions.

    Args:
        bubble_added_rows: result of SupabaseService.get_bubble_added_locations
            — list of {added_by, location_id, vibe_vector}.

    Returns:
        Dict mapping user_id → mean vibe vector (numpy array). Users with no
        usable contributions are absent from the dict.
    """
    if not bubble_added_rows:
        return {}

    grouped: Dict[str, List[np.ndarray]] = {}
    for row in bubble_added_rows:
        uid = row.get("added_by")
        if not uid:
            continue
        arr = _to_float_array(row.get("vibe_vector"))
        if arr is None:
            continue
        grouped.setdefault(uid, []).append(arr)

    intent_vecs: Dict[str, np.ndarray] = {}
    for uid, vecs in grouped.items():
        # Pad to a common length so np.mean over the stack works.
        max_len = max(v.size for v in vecs)
        stacked = np.stack([_pad_to(v, max_len) for v in vecs], axis=0)
        intent_vecs[uid] = stacked.mean(axis=0)

    return intent_vecs


def blend_personal_with_intent(
    personal_vec: Optional[List[float]],
    intent_vec: Optional[np.ndarray],
    intent_weight: float = INTENT_WEIGHT,
) -> Optional[np.ndarray]:
    """
    Blend a user's personal vibe vector with their bubble-intent vector.

    * No personal vector at all → return the intent vector if any, else None
    * No intent vector → return the personal vector unchanged
    * Both present → (1 - w) * personal + w * intent  (after length-padding)
    """
    personal_arr = _to_float_array(personal_vec)

    if personal_arr is None and intent_vec is None:
        return None
    if personal_arr is None:
        return intent_vec
    if intent_vec is None:
        return personal_arr

    max_len = max(personal_arr.size, intent_vec.size)
    p = _pad_to(personal_arr, max_len)
    i = _pad_to(intent_vec, max_len)
    return (1.0 - intent_weight) * p + intent_weight * i


def build_effective_user_vectors(
    user_vibe_vectors: Dict[str, Optional[List[float]]],
    bubble_added_rows: Optional[List[Dict[str, Any]]] = None,
    intent_weight: float = INTENT_WEIGHT,
) -> Dict[str, np.ndarray]:
    """
    For every user in the group, compute their effective vibe vector for
    this bubble.

    Args:
        user_vibe_vectors: {user_id: personal vibe_tag_affinity from `users` table}
        bubble_added_rows: rows from get_bubble_added_locations (or None / empty)
        intent_weight: blend factor (default 0.5)

    Returns:
        {user_id: effective vector (numpy)}. Users with neither a personal
        vector nor any contributions are absent from the dict.
    """
    intent_vecs = build_per_user_intent_vectors(bubble_added_rows or [])

    effective: Dict[str, np.ndarray] = {}
    for uid, personal in user_vibe_vectors.items():
        blended = blend_personal_with_intent(
            personal, intent_vecs.get(uid), intent_weight
        )
        if blended is not None:
            effective[uid] = blended
    return effective


def aggregate_group_vibe_vector(
    effective_vectors: Dict[str, np.ndarray],
    activity_weights: Dict[str, float],
) -> Optional[np.ndarray]:
    """
    Activity-weighted average of effective per-user vectors.

    Both inputs are keyed by user_id. Users present in `effective_vectors`
    but not in `activity_weights` get a default weight of 1/N.

    Returns the group vector or None if no users have a usable vector.
    """
    if not effective_vectors:
        return None

    n = len(effective_vectors)
    default_w = 1.0 / n

    max_len = max(v.size for v in effective_vectors.values())
    weighted_sum = np.zeros(max_len, dtype=float)
    total_weight = 0.0

    for uid, vec in effective_vectors.items():
        w = float(activity_weights.get(uid, default_w))
        if w <= 0:
            continue
        weighted_sum += w * _pad_to(vec, max_len)
        total_weight += w

    if total_weight <= 0:
        return None

    return weighted_sum / total_weight
