"""
Dietary scoring with hard-mismatch penalty.

Two outputs per (user, location) pair:

    dietary_match     – soft positive signal in [0, 1]; how well the location's
                        dietary capabilities align with the user's affinity.
    dietary_penalty   – hard multiplicative factor in [0, 1] applied to the
                        FINAL blended score. Drops to ~0 when a user with a
                        strong requirement (e.g. vegetarian = 100) lands on a
                        place that scores very low on that requirement.

The penalty is what stops a high-quality steakhouse from ever surfacing for
a vegetarian, no matter how good its app_engagement and google_baseline are.

Vector layout (see SupabaseService.dietary_requirements_order):
    0: halal
    1: vegan
    2: gluten-free
    3: vegetarian
    4: dairy-free
    5: nut-free

Both user (`dietary_requirement_tag_affinity`) and location
(`dietary_requirement_vector`) values are int 0..100.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence


# A user dim is "strong" if their affinity is at or above this value.
USER_STRONG_THRESHOLD = 50.0

# A location is considered "compatible enough" on a strong dim once it
# scores at this value (ramp linearly from 0 → COMPATIBLE_AT).
LOCATION_COMPATIBLE_AT = 30.0

# Floor of the multiplicative penalty so a single bad dim can't *quite*
# annihilate the score (keeps ranking stable when penalty interacts with
# the share_boost). 0.05 means a hard-mismatched place is ~20× worse than
# a fully-compatible one — effectively unrecommendable but not removed.
PENALTY_FLOOR = 0.05


def _coerce_vec(vec: Optional[Sequence[float]]) -> Optional[List[float]]:
    if not vec:
        return None
    try:
        return [float(x) for x in vec]
    except (TypeError, ValueError):
        return None


def _align(a: List[float], b: List[float]) -> tuple[List[float], List[float]]:
    """Pad the shorter vector with zeros so both have the same length."""
    if len(a) == len(b):
        return a, b
    n = max(len(a), len(b))
    return a + [0.0] * (n - len(a)), b + [0.0] * (n - len(b))


def compute_dietary_match(
    user_vec: Optional[Sequence[float]],
    location_vec: Optional[Sequence[float]],
) -> float:
    """
    Cosine-style dietary match in [0, 1].

    Unlike a raw dot product (which scales with vector magnitude and gives
    the dietary weight unpredictable behaviour), this normalises by the
    user's expressed needs so the score is "of the things you care about,
    how many can this place do?".

    Returns 0.0 when the user has no expressed dietary preferences (uniform
    or zero vector) — the recommender then falls through to other signals.
    """
    u = _coerce_vec(user_vec)
    l = _coerce_vec(location_vec)
    if not u or not l:
        return 0.0

    u, l = _align(u, l)
    user_norm = math.sqrt(sum(x * x for x in u))
    if user_norm == 0:
        return 0.0

    # Project location capability onto user's need direction, normalise to [0,1].
    # Each dim is 0..100; capping at 100 gives the right scale.
    raw = sum(min(li, 100.0) * ui for li, ui in zip(l, u))
    max_possible = 100.0 * user_norm * math.sqrt(len(u))
    if max_possible == 0:
        return 0.0
    return max(0.0, min(raw / max_possible, 1.0))


def compute_dietary_penalty(
    user_vec: Optional[Sequence[float]],
    location_vec: Optional[Sequence[float]],
    user_strong_threshold: float = USER_STRONG_THRESHOLD,
    location_compatible_at: float = LOCATION_COMPATIBLE_AT,
    penalty_floor: float = PENALTY_FLOOR,
) -> float:
    """
    Multiplicative penalty for fundamental dietary mismatch.

    Returns a value in [penalty_floor, 1.0]. For each dimension where the
    user's affinity >= user_strong_threshold (a *requirement*, not a
    preference), the location's score on that dim is mapped:

        location[d] >= location_compatible_at  → factor = 1.0
        location[d] == 0                       → factor = penalty_floor
        in between                             → linear ramp

    The factors multiply across dims, so a place that fails *two* of the
    user's requirements is doubly penalised. A user with no strong
    requirements gets factor 1.0 and the penalty is a no-op.
    """
    u = _coerce_vec(user_vec)
    l = _coerce_vec(location_vec)
    if not u or not l:
        return 1.0

    u, l = _align(u, l)
    factor = 1.0
    for ui, li in zip(u, l):
        if ui < user_strong_threshold:
            continue
        if li >= location_compatible_at:
            continue
        # Linear ramp from penalty_floor (li=0) to 1.0 (li=compatible_at)
        ramp = li / location_compatible_at
        dim_factor = penalty_floor + (1.0 - penalty_floor) * max(0.0, min(ramp, 1.0))
        factor *= dim_factor

    return max(penalty_floor, min(factor, 1.0))
