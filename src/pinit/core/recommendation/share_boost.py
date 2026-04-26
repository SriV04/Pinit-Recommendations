"""
Share boost: post-blend multiplier that lifts socially-shared places.

Applied AFTER the weighted blend so a place with even a single social-share
signal pulls clearly above the noise floor of pure quality/vibe matches.

Two inputs:
    share_count   – cached on location_popularity_app, union of:
                      * user_location_actions.action = 'shared_video'
                      * distinct video_insights.source_video_url rows
    video_insight_score – the precomputed 0..1 video pillar (used as a
                      gate: a location with a high video_insight score
                      gets a higher share_boost ceiling than one with the
                      same raw share_count but stale/negative videos).

The multiplier is bounded so a place with 100 shares but garbage quality
can't completely dominate ranking.
"""

from __future__ import annotations

import math


# Cap so the blend never gets multiplied by more than this factor.
# 1.6 means a maximally-shared place ranks ~60% higher than its raw blend.
MAX_BOOST = 1.6

# Per-share log coefficient. With ALPHA=0.30 and SHARE_GATE=1:
#   share_count = 1   → +0.21
#   share_count = 5   → +0.54
#   share_count = 25  → +0.98 (capped at MAX_BOOST - 1 = 0.6)
ALPHA = 0.30

# Below this share count there's no boost — keeps single accidental shares
# from elevating spam.
SHARE_GATE = 1


def compute_share_boost(
    share_count: int,
    video_insight_score: float = 0.0,
    alpha: float = ALPHA,
    max_boost: float = MAX_BOOST,
    share_gate: int = SHARE_GATE,
) -> float:
    """
    Return a multiplier in [1.0, max_boost] to apply to the blended score.

    The video_insight_score acts as a quality gate: places with a strong
    video pillar earn the full boost, places with no/negative video signal
    are dampened so spammy shares don't over-rank.
    """
    if share_count is None or share_count < share_gate:
        return 1.0

    # Log-saturated raw boost in [0, max_boost - 1]
    raw = alpha * math.log1p(share_count)

    # Quality gate: scale by (0.5 + 0.5 * video_insight_score) so a place
    # with no video signal still gets *half* of its raw boost (rewards the
    # share count itself), and a place with strong videos gets the full lift.
    gate = 0.5 + 0.5 * max(0.0, min(video_insight_score, 1.0))

    boost = 1.0 + min(raw * gate, max_boost - 1.0)
    return max(1.0, min(boost, max_boost))
