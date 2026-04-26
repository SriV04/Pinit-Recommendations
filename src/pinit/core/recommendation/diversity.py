"""
Per-user diversity layer applied AFTER scoring.

Three transforms, composed in order:

    1. apply_recently_seen_decay
         Pulls down candidates the user has already been shown recently
         (based on user_recommendations.generated_at). Exponential 7-day
         half-life. Same user, same query → different ordering after a few
         days, without thrash inside a single day.

    2. cuisine_mmr_rerank
         Maximal Marginal Relevance over cuisine_primary. Replaces the top-K
         "all Italian" cluster with a balanced mix while keeping each pick's
         per-item score high. Penalty per cuisine is cumulative.

    3. seeded_rank_jitter
         Deterministic ε-greedy swap within a small window in the top
         results. Seeded by (user_id, day) so within a session results are
         stable but day-to-day they shift. Off by default; opt-in.

These operate on the same dict shape as `_rank_cached_candidates`:
    {"location_id": int, "final_score": float, "cuisine_primary": str, ...}
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# ── Recently-seen decay ────────────────────────────────────────────────────

RECENTLY_SEEN_HALFLIFE_DAYS = 7.0
# Multiplier applied to seen-recently candidates: 1.0 = no penalty,
# 0.5 = halved score. We aim to demote, not delete.
RECENTLY_SEEN_FLOOR = 0.55


def apply_recently_seen_decay(
    candidates: List[Dict[str, Any]],
    seen_history: Dict[int, datetime],
    *,
    halflife_days: float = RECENTLY_SEEN_HALFLIFE_DAYS,
    floor: float = RECENTLY_SEEN_FLOOR,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Multiply final_score by (floor + (1 - floor) * (1 - exp(-Δt/halflife))).

    A candidate seen 0 days ago hits the floor; one not seen in 4× halflife
    is effectively un-decayed. The penalty is stored on the candidate as
    ``recently_seen_factor`` for debugging/explainability.
    """
    if not seen_history:
        return candidates

    now = now or datetime.now(timezone.utc)
    out = []
    for c in candidates:
        loc_id = c.get("location_id")
        ts = seen_history.get(loc_id) if loc_id is not None else None
        if ts is None:
            c["recently_seen_factor"] = 1.0
            out.append(c)
            continue

        # Treat naive timestamps as UTC
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        days = max((now - ts).total_seconds() / 86400.0, 0.0)
        decay = 1.0 - math.exp(-days / halflife_days)
        factor = floor + (1.0 - floor) * decay

        c["recently_seen_factor"] = factor
        c["final_score"] = c.get("final_score", 0.0) * factor
        out.append(c)

    out.sort(key=lambda x: x.get("final_score", 0.0), reverse=True)
    return out


# ── Cuisine MMR ────────────────────────────────────────────────────────────

def _cuisine_key(c: Dict[str, Any]) -> str:
    """Derive a coarse cuisine bucket. Falls back to cuisine, then 'other'."""
    raw = (
        c.get("cuisine_primary")
        or c.get("cuisine")
        or "other"
    )
    return str(raw).strip().lower() or "other"


def cuisine_mmr_rerank(
    candidates: List[Dict[str, Any]],
    *,
    lambda_: float = 0.7,
    k: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Maximal Marginal Relevance over cuisine_primary.

    For each step, pick the candidate maximising:
        lambda_ * score - (1 - lambda_) * cuisine_density(c)

    where cuisine_density(c) is how many already-selected items share the
    same cuisine bucket. lambda_=1.0 disables diversity (pure score).
    lambda_=0.5 is aggressive cuisine spread.

    Only the top-k positions are reranked; tail items keep score order.
    Items below position k are appended unchanged.
    """
    if not candidates:
        return candidates
    if lambda_ >= 0.999:
        return candidates

    candidates = sorted(
        candidates, key=lambda c: c.get("final_score", 0.0), reverse=True
    )
    k = k if k is not None else len(candidates)
    k = min(k, len(candidates))

    # Normalise scores into [0,1] so lambda blend is well-scaled
    max_score = max((c.get("final_score", 0.0) for c in candidates), default=1.0)
    if max_score <= 0:
        return candidates

    pool = list(candidates[:k])
    tail = list(candidates[k:])
    selected: List[Dict[str, Any]] = []
    cuisine_counts: Dict[str, int] = {}

    while pool:
        best_idx = 0
        best_val = -math.inf
        for i, cand in enumerate(pool):
            score_norm = cand.get("final_score", 0.0) / max_score
            density = cuisine_counts.get(_cuisine_key(cand), 0)
            # density penalty grows with the count of already-picked same-cuisine
            penalty = density / max(1, len(selected))
            val = lambda_ * score_norm - (1.0 - lambda_) * penalty
            if val > best_val:
                best_val = val
                best_idx = i

        chosen = pool.pop(best_idx)
        selected.append(chosen)
        cuisine_counts[_cuisine_key(chosen)] = cuisine_counts.get(
            _cuisine_key(chosen), 0
        ) + 1

    return selected + tail


# ── Seeded jitter (opt-in) ─────────────────────────────────────────────────

def seeded_rank_jitter(
    candidates: List[Dict[str, Any]],
    user_id: str,
    *,
    epsilon: float = 0.15,
    window: int = 3,
    top_n: int = 20,
    day: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Deterministic local swap inside the top_n positions.

    Seed = sha256(user_id || day). For each position 0..top_n-1, with
    probability epsilon swap with a position up to `window` ahead. Stable
    within a single (user, day) so a user reloading the page sees the same
    list, but the next day the same query produces a slightly different
    ordering.

    Pass `day` explicitly (UTC YYYY-MM-DD) for testing.
    """
    if not candidates or epsilon <= 0:
        return candidates

    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    seed_bytes = hashlib.sha256(f"{user_id}|{day}".encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(seed_bytes[:8], "big"))

    out = list(candidates)
    horizon = min(top_n, len(out))
    for i in range(horizon):
        if rng.random() >= epsilon:
            continue
        j = i + rng.randint(1, window)
        if j < horizon:
            out[i], out[j] = out[j], out[i]
    return out


# ── Convenience: full pipeline ─────────────────────────────────────────────

def diversify(
    candidates: List[Dict[str, Any]],
    *,
    seen_history: Optional[Dict[int, datetime]] = None,
    user_id: Optional[str] = None,
    cuisine_lambda: float = 0.7,
    enable_jitter: bool = False,
) -> List[Dict[str, Any]]:
    """Run decay → cuisine MMR → optional jitter, in order."""
    out = candidates
    if seen_history:
        out = apply_recently_seen_decay(out, seen_history)
    out = cuisine_mmr_rerank(out, lambda_=cuisine_lambda)
    if enable_jitter and user_id:
        out = seeded_rank_jitter(out, user_id)
    return out
