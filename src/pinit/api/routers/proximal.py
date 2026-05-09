from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import logging
import math
from time import perf_counter
from typing import Any, Dict, List, Optional
from uuid import uuid4

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from pinit.api.schemas import (
    AddLocationRequest,
    AddLocationResponse,
    BatchProximalRequest,
    BatchProximalResponse,
    BubbleLocationRecommendation,
    BubbleRequest,
    BubbleResponse,
    FriendSave,
    HealthResponse,
    HiddenGemLocation,
    HiddenGemsResponse,
    IndividualScore,
    LocationCoordinatesResponse,
    MagicLocationRecommendation,
    LocationRecommendation,
    MagicSearchRequest,
    MagicSearchResponse,
    MagicSearchSection,
    ProximalRequest,
    ProximalResponse,
    TagMatch,
    UserVibeScore,
)
from pinit.config.secrets import GOOGLE_PLACE_API_KEY
from pinit.api.services.proximal_service import (
    add_location_emoji,
    create_location_from_place_details,
    fetch_google_place_basic_details,
    refresh_location_from_google_place_details,
)
from pinit.api.schemas_magic import MagicIntent, MagicSearchDebug
from pinit.api.services.background_jobs import get_background_job_runner
from pinit.api.services.magic_google_service import get_or_fetch_google_candidates
from pinit.api.services.magic_intent_parser import normalise_prompt, parse_magic_intent
from pinit.api.schemas_location_tasks import PipelinePayload
from pinit.api.services.location_tasks import InProcessDispatcher, PubSubDispatcher, run_pipeline_inline
from pinit.api.services.cache_service import get_cache_service
from pinit.core.recommendation.magic_explanations import build_magic_sections
from pinit.core.recommendation.magic_ranking import (
    dedupe_magic_candidates,
    rerank_magic_candidates,
)
from pinit.core.recommendation.proximal_recommendation import (
    ProximalConfig,
    build_batch_proximal_recommendations,
    get_location_coordinates,
)
from pinit.core.recommendation.bubble_recommendation import (
    BubbleConfig,
    build_bubble_recommendations,
)
from pinit.integrations.pubsub_tasks import get_pubsub_config
from pinit.integrations.supabase import get_supabase_service

router = APIRouter()
logger = logging.getLogger(__name__)


def _photo_fields(row: Any) -> Dict[str, Any]:
    """Extract the three photo metadata booleans surfaced by get_locations_with_quality.

    Accepts a dict or a pandas row. Returns None for any field that's missing
    or NaN so Pydantic's Optional[...] validation is happy. The `photos`
    resource-name array is intentionally *not* returned by the bulk ranking
    RPC — a dedicated endpoint hydrates it when the client actually needs it.
    """
    def _get(key: str) -> Any:
        if isinstance(row, dict):
            return row.get(key)
        try:
            return row.get(key) if hasattr(row, "get") else row[key]
        except (KeyError, IndexError):
            return None

    def _clean(v: Any) -> Any:
        if v is None:
            return None
        try:
            if isinstance(v, float) and pd.isna(v):
                return None
        except Exception:
            pass
        return v

    raw_extra = _clean(_get("extra_photos_stored"))
    extra: Optional[int] = None
    if raw_extra is not None:
        try:
            extra = int(raw_extra)
        except (TypeError, ValueError):
            extra = None

    return {
        "image_stored": _clean(_get("image_stored")),
        "image_unavailable": _clean(_get("image_unavailable")),
        "extra_photos_stored": extra,
    }


VIBE_TAG_ORDER = {
    "cafe": 0, "casual": 1, "cozy": 2, "coffee_shop": 3, "bar": 4,
    "elegant": 5, "fine_dining": 6, "food_truck": 7, "hole_in_the_wall": 8, "late_night": 9,
    "live_music": 10, "michelin_starred": 11, "modern": 12, "fast_food": 13, "quiet": 14,
    "romantic": 15, "sports_bar": 16, "trendy": 17, "takeout_friendly": 18, "pub": 19,
    "grocery_store": 20, "brunch": 21, "outdoor_dining": 22, "wavy": 23, "bossman": 24,
}


def _safe_optional_str(value: Any) -> Optional[str]:
    """Convert pandas / numpy null-like values to None before response validation."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass

    text = value if isinstance(value, str) else str(value)
    return text or None


def _filter_candidates_by_tags(
    candidates: List[Dict[str, Any]],
    cuisine_filters: Optional[List[str]],
    vibe_filters: Optional[List[str]],
) -> List[Dict[str, Any]]:
    """
    Filter cached candidates by cuisine and vibe tags in-memory.

    Cuisine filter (OR logic): the candidate's ``cuisine`` or
    ``cuisine_secondary`` must match at least one of the filter values.

    Vibe filter (AND logic): for every requested vibe, the candidate's
    ``vibe_vector`` value at the corresponding index (looked up via
    VIBE_TAG_ORDER) must be above 50.

    Args:
        candidates: List of cached candidate dictionaries
        cuisine_filters: Optional list of cuisine names (OR logic)
        vibe_filters: Optional list of vibe tag names (AND logic)

    Returns:
        Filtered list of candidates
    """
    if not cuisine_filters and not vibe_filters:
        return candidates

    # Pre-compute vibe vector indices for the requested vibes
    vibe_indices: List[int] = []
    if vibe_filters:
        for vibe_name in vibe_filters:
            idx = VIBE_TAG_ORDER.get(vibe_name)
            if idx is not None:
                vibe_indices.append(idx)

    # Normalise cuisine filters to lowercase for case-insensitive matching
    cuisine_set = {c.lower() for c in cuisine_filters} if cuisine_filters else None

    filtered = []
    for candidate in candidates:
        # Cuisine filter (OR logic) - cuisine or cuisine_secondary must match
        if cuisine_set:
            primary = (
                _safe_optional_str(candidate.get("cuisine"))
                or _safe_optional_str(candidate.get("cuisine_primary"))
                or ""
            ).lower()
            secondary = (_safe_optional_str(candidate.get("cuisine_secondary")) or "").lower()
            if primary not in cuisine_set and secondary not in cuisine_set:
                continue

        # Vibe filter (AND logic) - every requested vibe must score above 50
        if vibe_indices:
            vibe_vector = candidate.get("vibe_vector")
            if not vibe_vector:
                continue
            if not all(
                idx < len(vibe_vector) and (vibe_vector[idx] or 0) > 50
                for idx in vibe_indices
            ):
                continue

        filtered.append(candidate)

    return filtered


def _diversify_by_top_tags(
    scored_candidates: List[Dict[str, Any]],
    user_vibe_vec: List[int],
    vibe_tag_order: Dict[str, int],
    max_results: int,
    top_n_tags: int = 5,
) -> List[Dict[str, Any]]:
    """
    Apply proportional diversity sampling across the user's top vibe tags.

    Instead of returning the top max_results purely by final_score (which clusters
    results around the single highest-affinity tag), this allocates result slots to each
    of the user's top N tags proportionally to their affinity scores.

    Example: if the user's top tags are cafe:85, bossman:70, fine-dining:65, sports-bar:63,
    wavy:63 and max_results=20, slots are allocated as ~6 cafe, ~5 bossman, ~4 fine-dining,
    ~3 sports-bar, ~2 wavy. Within each bucket the candidates with the highest tag-specific
    score are selected. The final list is re-sorted by final_score.

    Args:
        scored_candidates: All candidates, already sorted by final_score descending.
        user_vibe_vec: User's vibe affinity vector (int values 0-100 per tag dimension).
        vibe_tag_order: Mapping of tag text → vector index (from SupabaseService).
        max_results: Total number of results to return.
        top_n_tags: Number of top user tags to distribute slots across.

    Returns:
        Diverse list of up to max_results candidates, sorted by final_score.
    """
    index_to_tag = {idx: tag for tag, idx in vibe_tag_order.items()}

    # Extract user's top N tags (by affinity score, skip zeros)
    user_arr = np.array(user_vibe_vec, dtype=float)
    tag_scores = [
        (index_to_tag[i], float(user_arr[i]))
        for i in range(len(user_arr))
        if i in index_to_tag and float(user_arr[i]) > 0
    ]
    tag_scores.sort(key=lambda x: x[1], reverse=True)
    top_tags = tag_scores[:top_n_tags]

    # Fall back to plain top-N if user has no meaningful preferences
    if not top_tags:
        return scored_candidates[:max_results]

    total_weight = sum(score for _, score in top_tags)

    # Compute proportional slot counts using largest-remainder method
    raw_slots = [(tag, score / total_weight * max_results) for tag, score in top_tags]
    floor_slots = {tag: int(s) for tag, s in raw_slots}
    remainder = max_results - sum(floor_slots.values())
    fractional_order = sorted(
        raw_slots, key=lambda x: x[1] - int(x[1]), reverse=True
    )
    for i in range(remainder):
        floor_slots[fractional_order[i][0]] += 1
    slot_map = floor_slots  # tag -> slot count

    selected_ids: set = set()
    result: List[Dict[str, Any]] = []

    for tag_name, slot_count in slot_map.items():
        if slot_count == 0:
            continue
        tag_idx = vibe_tag_order.get(tag_name)
        if tag_idx is None:
            continue

        # Score each un-selected candidate by how well it matches this specific tag,
        # blended with its overall final_score so quality still matters.
        def _tag_affinity_score(c: Dict[str, Any], idx: int = tag_idx) -> float:
            loc_vec = c.get("vibe_vector")
            if not loc_vec or idx >= len(loc_vec):
                tag_raw = 0.0
            else:
                tag_raw = float(loc_vec[idx]) / 100.0  # normalize to [0, 1]
            return 0.7 * tag_raw + 0.3 * c.get("final_score", 0.0)

        candidates_for_tag = [
            c for c in scored_candidates
            if c.get("location_id") not in selected_ids
        ]
        candidates_for_tag.sort(key=_tag_affinity_score, reverse=True)

        for candidate in candidates_for_tag[:slot_count]:
            selected_ids.add(candidate.get("location_id"))
            result.append(candidate)

    # Fill any remaining slots with the best overall-scored candidates not yet picked
    if len(result) < max_results:
        remaining = [
            c for c in scored_candidates
            if c.get("location_id") not in selected_ids
        ]
        result.extend(remaining[: max_results - len(result)])

    # Re-sort by final_score so the ordering is still sensible
    result.sort(key=lambda x: x.get("final_score", 0.0), reverse=True)
    return result[:max_results]


def _rank_cached_candidates(
    candidates: List[Dict[str, Any]],
    request_lat: float,
    request_lng: float,
    request_radius_km: float,
    quality_weight: float,
    vibe_weight: float,
    dietary_weight: float,
    max_results: int,
    cuisine_filters: Optional[List[str]] = None,
    vibe_filters: Optional[List[str]] = None,
    user_id: Optional[str] = None,
    social_weight: float = 0.18,
    collaborative_weight: float = 0.12,
    group_context: Optional[Dict[str, Any]] = None,
    *,
    # New v4 pillar weights — keyword-only so existing callers keep working
    # by treating the legacy `quality_weight` as the SUM of these two.
    app_engagement_weight: Optional[float] = None,
    google_baseline_weight: Optional[float] = None,
    video_insight_weight: float = 0.10,
    cuisine_diversity_lambda: float = 0.7,
    enable_seen_decay: bool = True,
    enable_rank_jitter: bool = False,
    # Bundled user profile from cache_service. Avoids per-request
    # round-trips for vectors / friends / action_count.
    user_profile: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Apply filtering and ranking to cached candidates using 5-component scoring.

    Computes user-specific vibe, dietary, social, and collaborative scores on
    the fly. Quality scores are pre-computed in the database and cached.

    Args:
        candidates: List of cached candidate dictionaries
        request_lat, request_lng: Request center coordinates
        request_radius_km: Request radius for filtering
        quality_weight, vibe_weight, dietary_weight: Original scoring weights
        max_results: Maximum number of results to return
        cuisine_filters: Optional cuisine tag filters (OR logic)
        vibe_filters: Optional vibe tag filters (AND logic)
        user_id: User ID for computing user-specific scores
        social_weight: Weight for friend-based social score
        collaborative_weight: Weight for collaborative filtering score

    Returns:
        List of scored and ranked candidates (includes weights_used metadata)
    """
    from pinit.core.recommendation.proximal_recommendation import (
        compute_adaptive_weights,
        haversine_distance,
    )
    from pinit.core.recommendation.vector_utils import centered_cosine_similarity, dot_product
    from pinit.core.recommendation.social_scoring import compute_social_scores
    from pinit.core.recommendation.collaborative_scoring import compute_collaborative_scores
    from pinit.core.recommendation.dietary_scoring import (
        compute_dietary_match,
        compute_dietary_penalty,
    )
    from pinit.core.recommendation.share_boost import compute_share_boost
    from pinit.core.recommendation.diversity import diversify

    # Resolve v4 pillar weights from the legacy single quality_weight if the
    # caller hasn't supplied the split values explicitly.
    if app_engagement_weight is None:
        app_engagement_weight = quality_weight * (5.0 / 6.0)
    if google_baseline_weight is None:
        google_baseline_weight = quality_weight * (1.0 / 6.0)

    supabase = get_supabase_service()

    # ── Mode resolution ────────────────────────────────────────────────
    # PROXIMAL mode: user_id is set, group_context is None
    # BUBBLE mode:   group_context is set, user_id may be None
    #
    # group_context shape (all keys required when set):
    #   {
    #     "group_vibe_vec":           List[float] | np.ndarray,    # used for the
    #                                                              # vibe_score
    #     "user_dietary_vectors":     Dict[str, List[float]],      # per user
    #     "user_effective_vibe_vecs": Dict[str, np.ndarray],       # per user,
    #                                                              # for the
    #                                                              # individual
    #                                                              # breakdown
    #     "activity_weights":         Dict[str, float],            # per user
    #     "bubble_location_ids":      Set[int],                    # for boost
    #     "bubble_boost":             float,                       # e.g. 0.1
    #   }
    is_group_mode = group_context is not None

    # User vectors come from the bundled profile when available; fall back
    # to a slim DB query for legacy callers (group mode, batch endpoint).
    user_vibe_vec = None
    user_dietary_vec = None
    vibe_tag_order = supabase.vibe_tag_order  # used by diversification in both modes
    if user_id and not is_group_mode:
        if user_profile is not None:
            user_vibe_vec = user_profile.get("vibe_vector")
            user_dietary_vec = user_profile.get("dietary_vector")
        else:
            user_data = supabase.get_user_vectors(user_id)
            if user_data:
                user_vibe_vec = user_data.get("vibe_tag_affinity")
                user_dietary_vec = user_data.get("dietary_requirement_tag_affinity")

    # Step 1: Filter by cuisine and vibe tags if specified
    filtered_candidates = _filter_candidates_by_tags(
        candidates, cuisine_filters, vibe_filters
    )

    # Step 2: Filter by radius first to get candidate location IDs
    radius_filtered = []
    for candidate in filtered_candidates:
        lat = candidate.get("lat")
        lng = candidate.get("lng")
        if lat is None or lng is None:
            continue
        distance_km = haversine_distance(request_lat, request_lng, lat, lng)
        if distance_km <= request_radius_km:
            candidate["distance_km"] = distance_km
            radius_filtered.append(candidate)

    if not radius_filtered:
        logger.info(
            "🧭 _rank_cached_candidates: 0 candidates survived radius filter "
            "(%d input, radius=%.1f km)",
            len(candidates),
            request_radius_km,
        )
        return []

    # Diagnostic: how many of the radius-filtered candidates have a
    # populated quality_score? If this is 0 while the count is large, the
    # cache payload was written before location_popularity_app was seeded.
    rf_qs_present = sum(
        1 for c in radius_filtered if (c.get("quality_score") or 0) > 0
    )
    rf_qs_max = max(
        (float(c.get("quality_score") or 0) for c in radius_filtered), default=0.0
    )
    logger.info(
        "🧭 _rank_cached_candidates: %d candidates in radius %.1f km "
        "(input=%d, tag-filtered=%d, quality_score>0: %d, max=%.3f)",
        len(radius_filtered),
        request_radius_km,
        len(candidates),
        len(filtered_candidates),
        rf_qs_present,
        rf_qs_max,
    )

    candidate_location_ids = [c["location_id"] for c in radius_filtered]

    # Step 3: Compute social and collaborative scores in batch (user-specific).
    # Skipped entirely in group mode — bubble recs ignore both signals.
    social_scores: Dict[int, float] = {}
    friend_attributions: Dict[int, list] = {}
    collab_scores: Dict[int, float] = {}
    has_friends = False
    action_count = 0

    if user_id and not is_group_mode:
        # Social scoring
        social_scores, friend_attributions = compute_social_scores(
            user_id, candidate_location_ids, supabase
        )
        has_friends = any(s > 0 for s in social_scores.values())

        # Collaborative scoring
        collab_scores = compute_collaborative_scores(
            user_id, candidate_location_ids, supabase
        )

        # Get action count for adaptive weights — prefer the bundled profile
        if user_profile is not None and "action_count" in user_profile:
            action_count = int(user_profile.get("action_count") or 0)
        else:
            action_count = supabase.get_user_action_count(user_id)

    # Step 4: Compute adaptive weights. In group mode, force social/collab to 0
    # so the request weights are renormalised across just quality / vibe / dietary.
    if is_group_mode:
        effective_social_w = 0.0
        effective_collab_w = 0.0
    else:
        effective_social_w = social_weight
        effective_collab_w = collaborative_weight

    weights = compute_adaptive_weights(
        has_friends=has_friends,
        action_count=action_count,
        app_engagement_w=app_engagement_weight,
        google_baseline_w=google_baseline_weight,
        video_insight_w=video_insight_weight,
        vibe_w=vibe_weight,
        dietary_w=dietary_weight,
        social_w=effective_social_w,
        collaborative_w=effective_collab_w,
    )

    w_app_engagement = weights["app_engagement"]
    w_google_baseline = weights["google_baseline"]
    w_video_insight = weights["video_insight"]
    w_vibe = weights["vibe"]
    w_dietary = weights["dietary"]
    w_social = weights["social"]
    w_collab = weights["collaborative"]

    # Step 5: Score each candidate.
    #
    # PROXIMAL mode: 5 components (quality, vibe, dietary, social, collab)
    # BUBBLE mode:   3 components (quality, vibe, dietary). Vibe is one
    #                cosine against the group's blended vector. Dietary is
    #                MAX-pooled across per-user dot products. Each candidate
    #                also gets `individual_vibe_scores` and `is_in_bubble`.
    group_vibe_vec_local = (
        group_context.get("group_vibe_vec") if is_group_mode else None
    )
    user_dietary_vectors = (
        group_context.get("user_dietary_vectors", {}) if is_group_mode else {}
    )
    user_effective_vibe_vecs = (
        group_context.get("user_effective_vibe_vecs", {}) if is_group_mode else {}
    )
    bubble_location_ids = (
        group_context.get("bubble_location_ids", set()) if is_group_mode else set()
    )

    scored_candidates = []
    for candidate in radius_filtered:
        loc_id = candidate["location_id"]
        loc_vibe_vec = candidate.get("vibe_vector")
        loc_dietary_vec = candidate.get("dietary_requirement_vector")

        # Pillar scores precomputed by the v6 cron. If we're handed a legacy
        # cache row with only `quality_score`, mirror it into both
        # replacement pillars so the split weights preserve the old overall
        # contribution.
        app_engagement = float(candidate.get("app_engagement_score") or 0.0)
        google_baseline = float(candidate.get("google_baseline_score") or 0.0)
        video_insight = float(candidate.get("video_insight_score") or 0.0)
        share_count = int(candidate.get("share_count") or 0)

        if (
            candidate.get("app_engagement_score") is None
            and candidate.get("google_baseline_score") is None
            and candidate.get("quality_score") is not None
        ):
            legacy_quality = float(candidate.get("quality_score") or 0.0)
            app_engagement = legacy_quality
            google_baseline = legacy_quality

        quality_score = candidate.get("quality_score")
        if quality_score is None:
            quality_score = (
                app_engagement * (5.0 / 6.0)
                + google_baseline * (1.0 / 6.0)
            )
        else:
            quality_score = float(quality_score or 0.0)

        # Manual additive bias — set by hand on location_popularity_app.
        # Range [-0.15, +0.15]. Applied AFTER the weighted blend and all
        # multipliers so a manual nudge always reaches the final score
        # regardless of dietary penalty / share boost / fill factor.
        quality_bias = float(candidate.get("quality_bias") or 0.0)

        vibe_score = 0.0
        dietary_score = 0.0
        dietary_penalty = 1.0
        individual_vibe_scores: List[Dict[str, Any]] = []

        if is_group_mode:
            # ── group vibe: one cosine against the blended group vector
            if group_vibe_vec_local is not None and loc_vibe_vec:
                vibe_score = centered_cosine_similarity(
                    list(group_vibe_vec_local), loc_vibe_vec
                )

            # ── group dietary: MAX match, MIN penalty across users.
            # MIN penalty: if any group member would be hard-mismatched,
            # the place is penalised for the whole group.
            if user_dietary_vectors and loc_dietary_vec:
                per_user_match = []
                per_user_penalty = []
                for uid, uvec in user_dietary_vectors.items():
                    if uvec:
                        per_user_match.append(compute_dietary_match(uvec, loc_dietary_vec))
                        per_user_penalty.append(compute_dietary_penalty(uvec, loc_dietary_vec))
                if per_user_match:
                    dietary_score = max(per_user_match)
                if per_user_penalty:
                    dietary_penalty = min(per_user_penalty)

            # ── per-user vibe breakdown using each user's effective vector
            if loc_vibe_vec:
                for uid, eff_vec in user_effective_vibe_vecs.items():
                    individual_vibe_scores.append({
                        "user_id": uid,
                        "vibe_score": centered_cosine_similarity(
                            list(eff_vec), loc_vibe_vec
                        ),
                    })
        else:
            # Single-user proximal flow
            if user_id:
                if user_vibe_vec and loc_vibe_vec:
                    vibe_score = centered_cosine_similarity(user_vibe_vec, loc_vibe_vec)
                if user_dietary_vec and loc_dietary_vec:
                    dietary_score = compute_dietary_match(user_dietary_vec, loc_dietary_vec)
                    dietary_penalty = compute_dietary_penalty(user_dietary_vec, loc_dietary_vec)

        social_score = social_scores.get(loc_id, 0.0)
        collaborative_score = collab_scores.get(loc_id, 0.0)

        blended = (
            w_app_engagement  * app_engagement +
            w_google_baseline * google_baseline +
            w_video_insight   * video_insight +
            w_vibe            * vibe_score +
            w_dietary         * dietary_score +
            w_social          * social_score +
            w_collab          * collaborative_score
        )
        share_boost = compute_share_boost(share_count, video_insight)

        # Fill-set demotion: locations without a `location_popularity_app`
        # entry have no real engagement signal yet. They're included as
        # geographic fill but should rank below known places at parity.
        # 0.6 multiplier keeps them visible (so genuinely better fills can
        # still surface) but pushes them out of the top slots when known
        # places are competitive.
        has_app_signal = bool(candidate.get("has_app_signal", True))
        fill_factor = 1.0 if has_app_signal else 0.6
        # quality_bias is purely additive — applied AFTER all multipliers
        # so a manual nudge can't be cancelled out by dietary penalty etc.
        final_score = (
            blended * dietary_penalty * share_boost * fill_factor
            + quality_bias
        )

        scored_candidates.append({
            **candidate,
            "vibe_score": vibe_score,
            "dietary_score": dietary_score,
            "dietary_penalty": dietary_penalty,
            "app_engagement_score": app_engagement,
            "google_baseline_score": google_baseline,
            "video_insight_score": video_insight,
            "quality_score": quality_score,
            "share_count": share_count,
            "share_boost": share_boost,
            "has_app_signal": has_app_signal,
            "fill_factor": fill_factor,
            "quality_bias": quality_bias,
            "social_score": social_score,
            "collaborative_score": collaborative_score,
            "final_score": final_score,
            "friend_saves": friend_attributions.get(loc_id),
            "weights_used": weights,
            "individual_vibe_scores": individual_vibe_scores,
            "is_in_bubble": loc_id in bubble_location_ids,
        })

    # Step 6: Sort by final score
    scored_candidates.sort(key=lambda x: x.get("final_score", 0), reverse=True)

    # Step 7: Diversify across the seed vibe vector's top tags, then re-sort.
    # In group mode that's the blended group vector; in proximal it's the
    # individual user's vibe_tag_affinity.
    if is_group_mode and group_vibe_vec_local is not None:
        diversify_seed = list(group_vibe_vec_local)
    else:
        diversify_seed = user_vibe_vec

    if diversify_seed and vibe_tag_order:
        diverse_candidates = _diversify_by_top_tags(
            scored_candidates, diversify_seed, vibe_tag_order, max_results
        )
    else:
        diverse_candidates = scored_candidates[:max_results]

    # Step 7b: Bubble boost — locations the group has already added get a
    # small additive bonus so they surface near the top of their own list,
    # without dominating discovery. Only applied in group mode.
    if is_group_mode and bubble_location_ids:
        bubble_boost = float(group_context.get("bubble_boost", 0.1))
        for c in diverse_candidates:
            if c.get("is_in_bubble"):
                c["final_score"] = c.get("final_score", 0.0) + bubble_boost
        diverse_candidates.sort(
            key=lambda x: x.get("final_score", 0.0), reverse=True
        )

    # Step 7c: Per-user diversity — recently-seen decay + cuisine MMR.
    # Skipped in group mode (the group already diversifies via top-tags
    # and a bubble may legitimately want repeat picks).
    if not is_group_mode and user_id:
        seen_history: Dict[int, Any] = {}
        if enable_seen_decay:
            try:
                seen_history = supabase.get_recent_recommendation_timestamps(
                    user_id, [c.get("location_id") for c in diverse_candidates]
                )
            except AttributeError:
                # Helper not yet implemented on SupabaseService.
                seen_history = {}
            except Exception:
                logger.exception("Failed to fetch seen_history; skipping decay")
                seen_history = {}

        diverse_candidates = diversify(
            diverse_candidates,
            seen_history=seen_history,
            user_id=user_id,
            cuisine_lambda=cuisine_diversity_lambda,
            enable_jitter=enable_rank_jitter,
        )

    # Step 8: Assign ranks
    for rank, candidate in enumerate(diverse_candidates, start=1):
        candidate["rank"] = rank

    return diverse_candidates


@router.get("/", response_model=Dict[str, str])
async def root() -> Dict[str, str]:
    """Root endpoint."""
    return {
        "message": "Pinit Proximal Recommendations API",
        "version": "1.0.0",
        "docs": "/docs",
    }


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Health check endpoint."""
    supabase = get_supabase_service()

    # Query database for counts (lightweight count-only queries)
    try:
        count_locations_for_health = getattr(
            supabase,
            "count_locations_for_health",
            supabase.count_locations,
        )
        locations_count = count_locations_for_health()
        users_count = supabase.count_users()
        tags_count = supabase.count_tags()

        return HealthResponse(
            status="healthy",
            timestamp=datetime.utcnow().isoformat(),
            data_loaded=True,
            total_locations=locations_count,
            total_users=users_count,
            total_tags=tags_count,
        )
    except Exception as exc:
        logger.error(f"Health check failed: {exc}")
        return HealthResponse(
            status="unhealthy",
            timestamp=datetime.utcnow().isoformat(),
            data_loaded=False,
            total_locations=0,
            total_users=0,
            total_tags=0,
        )


@router.get("/hidden-gems", response_model=HiddenGemsResponse)
async def get_hidden_gems(
    latitude: float = Query(..., ge=-90, le=90),
    longitude: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(2.0, gt=0, le=50),
    max_results: int = Query(20, ge=1, le=100),
    min_reviews: int = Query(35, ge=1),
    min_quality_score: float = 0.6,
) -> HiddenGemsResponse:
    """
    Get hidden gem restaurants within a radius.

    Returns highly-rated but under-the-radar locations scored by quality relative
    to popularity: high quality_score * low review count = likely hidden gem.
    """
    import math
    from pinit.core.recommendation.proximal_recommendation import haversine_distance

    supabase = get_supabase_service()
    nearby_data = supabase.get_locations_with_quality_scores(
        latitude, longitude, radius_km, limit=1000
    )

    empty_response = HiddenGemsResponse(
        center_lat=latitude,
        center_lon=longitude,
        radius_km=radius_km,
        total_results=0,
        min_reviews=min_reviews,
        recommendations=[],
        timestamp=datetime.utcnow().isoformat(),
    )

    if not nearby_data:
        return empty_response

    # Filter by radius and minimum review count
    eligible = []
    for loc in nearby_data:
        lat = loc.get("lat")
        lng = loc.get("lng")
        if lat is None or lng is None:
            continue
        dist = haversine_distance(latitude, longitude, lat, lng)
        if dist > radius_km:
            continue
        if (loc.get("user_ratings_total") or 0) < min_reviews:
            continue
        if float(loc.get("quality_score") or 0.0) < min_quality_score:
            continue
        eligible.append({**loc, "distance_km": dist})

    if not eligible:
        return empty_response

    # Score: quality * (1 - normalised popularity)
    # Normalise log(reviews) within the candidate set so the penalty is relative
    log_reviews = [math.log1p(c.get("user_ratings_total") or 0) for c in eligible]
    min_lr = min(log_reviews)
    max_lr = max(log_reviews)

    for i, candidate in enumerate(eligible):
        quality = float(candidate.get("quality_score") or 0.0)
        norm_pop = (log_reviews[i] - min_lr) / (max_lr - min_lr) if max_lr > min_lr else 0.0
        candidate["hidden_gem_score"] = quality * (1.0 - norm_pop)

    eligible.sort(key=lambda x: x["hidden_gem_score"], reverse=True)

    recommendations = []
    for rank, loc in enumerate(eligible[:max_results], start=1):
        recommendations.append(
            HiddenGemLocation(
                location_id=int(loc["location_id"]),
                name=loc["name"],
                vicinity=_safe_optional_str(loc.get("vicinity")),
                cuisine_primary=_safe_optional_str(loc.get("cuisine_primary")),
                rating=float(loc["rating"]) if loc.get("rating") is not None else None,
                user_ratings_total=int(loc["user_ratings_total"]) if loc.get("user_ratings_total") is not None else None,
                price_level=float(loc["price_level"]) if loc.get("price_level") is not None else None,
                distance_km=float(loc["distance_km"]),
                quality_score=float(loc.get("quality_score") or 0.0),
                hidden_gem_score=float(loc["hidden_gem_score"]),
                rank=rank,
                **_photo_fields(loc),
            )
        )

    return HiddenGemsResponse(
        center_lat=latitude,
        center_lon=longitude,
        radius_km=radius_km,
        total_results=len(recommendations),
        min_reviews=min_reviews,
        recommendations=recommendations,
        timestamp=datetime.utcnow().isoformat(),
    )


@router.post("/recommendations/proximal", response_model=ProximalResponse)
async def get_proximal_recommendations(request: ProximalRequest) -> ProximalResponse:
    """
    Get personalized location recommendations within a radius.

    Returns recommendations sorted by a weighted score combining:
    - User taste preferences
    - Proximity to center point
    - Location quality (ratings)
    """
    supabase = get_supabase_service()
    cache_service = get_cache_service()

    # Validate user exists — single bundled fetch (cached) replaces the
    # legacy get_user + get_user_vectors + get_user_friends + action_count
    # round-trips on the warm-cache path.
    user_profile = cache_service.get_or_build_user_profile(
        request.user_id, supabase
    )
    if user_profile is None:
        raise HTTPException(
            status_code=404,
            detail=f"User '{request.user_id}' not found."
        )

    # Log incoming request parameters
    logger.info(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    logger.info(
        "📍 PROXIMAL REQUEST: user=%s, position=(%.4f, %.4f), radius=%.1f km, max_results=%d",
        request.user_id,
        request.latitude,
        request.longitude,
        request.radius_km,
        request.max_results,
    )
    logger.info(
        "⚖️  Weights: quality=%.2f, vibe=%.2f, dietary=%.2f, social=%.2f, collab=%.2f",
        request.quality_weight,
        request.vibe_weight,
        request.dietary_weight,
        request.social_weight,
        request.collaborative_weight,
    )

    # Extract optional filters
    cuisine_filters = None
    vibe_filters = None

    if request.filters:
        cuisine_filters = request.filters.cuisine
        vibe_filters = request.filters.vibe
        logger.info(
            "🔍 Filters: cuisine=%s, vibe=%s",
            cuisine_filters if cuisine_filters else "None",
            vibe_filters if vibe_filters else "None",
        )
    else:
        logger.info("🔍 Filters: None")

    # Create config
    config = ProximalConfig(
        radius_km=request.radius_km,
        max_results=request.max_results,
        quality_weight=request.quality_weight,
        vibe_weight=request.vibe_weight,
        dietary_weight=request.dietary_weight,
        social_weight=request.social_weight,
        collaborative_weight=request.collaborative_weight,
    )

    # Try cache first (user-agnostic - shared across all users).
    # cache_service was already initialised above for the user-profile
    # fetch; reuse it here.
    cached_data = cache_service.get_cached_recommendations(
        request.latitude, request.longitude, request.radius_km
    )

    if cached_data is not None:
        # Cache hit - use shared filtering logic
        # Cached candidates include the v6 pillar payload plus a legacy
        # `quality_score` alias for diagnostics / response compatibility.
        candidates = cached_data.get("candidates", [])

        # Diagnostics on cached quality scores — if the cache was populated
        # before the v4/v5 cron seeded location_popularity_app, every value
        # here will be 0 until the cache entry expires (2h TTL).
        cached_qs_present = sum(
            1 for c in candidates if (c.get("quality_score") or 0) > 0
        )
        cached_qs_max = max(
            (float(c.get("quality_score") or 0) for c in candidates), default=0.0
        )
        logger.info(
            "✅ CACHE HIT: Retrieved %d candidates from cache at (%.4f, %.4f) "
            "(user-agnostic, quality_score>0: %d, max=%.3f)",
            len(candidates),
            cached_data.get("center_lat"),
            cached_data.get("center_lng"),
            cached_qs_present,
            cached_qs_max,
        )

        # Apply shared filtering and ranking logic (compute user-specific scores)
        scored_candidates = _rank_cached_candidates(
            candidates=candidates,
            request_lat=request.latitude,
            request_lng=request.longitude,
            request_radius_km=request.radius_km,
            quality_weight=request.quality_weight,
            vibe_weight=request.vibe_weight,
            dietary_weight=request.dietary_weight,
            max_results=request.max_results,
            cuisine_filters=cuisine_filters,
            vibe_filters=vibe_filters,
            user_id=request.user_id,
            social_weight=request.social_weight,
            collaborative_weight=request.collaborative_weight,
            user_profile=user_profile,
        )

        logger.info(
            "📊 Filtered and ranked: %d results returned from %d cached candidates",
            len(scored_candidates),
            len(candidates)
        )

        # Log sample result
        if scored_candidates:
            sample = scored_candidates[0]
            logger.debug(
                "Top result: location_id=%s, distance=%.2f km, "
                "vibe=%.2f, dietary=%.2f, quality=%.2f, social=%.2f, collab=%.2f, final=%.2f",
                sample.get("location_id"),
                sample.get("distance_km"),
                sample.get("vibe_score"),
                sample.get("dietary_score"),
                sample.get("quality_score"),
                sample.get("social_score"),
                sample.get("collaborative_score"),
                sample.get("final_score")
            )

        # Convert to DataFrame for consistent processing
        recs = pd.DataFrame(scored_candidates) if scored_candidates else pd.DataFrame()

        if not recs.empty:
            recs = recs.replace({pd.NA: None, float('nan'): None, np.nan: None})

    else:
        # Cache miss - query locations with quality scores pre-computed
        cache_radius_km = 15.0  # Large radius for caching
        logger.info(
            "❌ CACHE MISS at (%.4f, %.4f), querying locations within %.1f km with quality scores (PostGIS)",
            request.latitude,
            request.longitude,
            cache_radius_km
        )

        supabase = get_supabase_service()

        # ── Try the LPA snapshot first (engaged places, 1 small Redis read).
        # If present, the SQL fetch only needs to return the fill_set —
        # locations not in LPA — which is a much smaller payload.
        nearby_locations_data: List[Dict[str, Any]] = []
        lpa_snapshot = cache_service.get_lpa_snapshot()
        if lpa_snapshot:
            primary_set = cache_service.filter_lpa_snapshot_by_radius(
                lpa_snapshot, request.latitude, request.longitude, cache_radius_km
            )
            fill_set = supabase.get_fill_locations(
                request.latitude,
                request.longitude,
                cache_radius_km,
                limit=max(1000 - len(primary_set), 0),
            )
            nearby_locations_data = primary_set + fill_set
            logger.info(
                "🧩 LPA snapshot path: %d engaged + %d fill = %d total",
                len(primary_set),
                len(fill_set),
                len(nearby_locations_data),
            )

        # Fallback: snapshot missing or fill RPC unavailable → legacy bulk RPC.
        if not nearby_locations_data:
            nearby_locations_data = supabase.get_locations_with_quality_scores(
                request.latitude,
                request.longitude,
                cache_radius_km,
                limit=1000,
            )

        if not nearby_locations_data:
            logger.warning("No locations found within %s km, returning empty results", cache_radius_km)
            return ProximalResponse(
                user_id=request.user_id,
                center_lat=request.latitude,
                center_lon=request.longitude,
                radius_km=request.radius_km,
                total_results=0,
                recommendations=[],
                timestamp=datetime.utcnow().isoformat(),
            )

        # Convert to DataFrame - vectors, pillar scores, quality_score alias,
        # and quality_bias are already included.
        nearby_locations_df = pd.DataFrame(nearby_locations_data)

        # Diagnostics: how many of the spatial results actually have a
        # populated quality_score? If this is 0 or very low, the v4/v5
        # refresh job hasn't populated location_popularity_app for this area.
        qs_col = nearby_locations_df.get("quality_score")
        if qs_col is not None:
            qs_numeric = pd.to_numeric(qs_col, errors="coerce").fillna(0.0)
            qs_present = int((qs_numeric > 0).sum())
            qs_missing = int((qs_numeric <= 0).sum())
            qs_max = float(qs_numeric.max()) if len(qs_numeric) else 0.0
            qs_mean = float(qs_numeric.mean()) if len(qs_numeric) else 0.0
        else:
            qs_present = qs_missing = 0
            qs_max = qs_mean = 0.0

        logger.info(
            "📍 Spatial query found %d locations within %.1f km "
            "(quality_score>0: %d, quality_score<=0: %d, max=%.3f, mean=%.3f)",
            len(nearby_locations_df),
            cache_radius_km,
            qs_present,
            qs_missing,
            qs_max,
            qs_mean,
        )

        # Cache locations with quality scores (user-agnostic)
        if not nearby_locations_df.empty:
            cache_cols = [
                'location_id', 'name', 'vicinity', 'cuisine_primary',
                'rating', 'user_ratings_total', 'price_level',
                'lat', 'lng', 'distance_km',
                'vibe_vector', 'dietary_requirement_vector',  # Need vectors for user-specific scoring
                'app_engagement_score', 'google_baseline_score',
                'video_insight_score', 'share_count',
                'has_app_signal',
                'quality_score', 'quality_bias',
                # Photo metadata surfaced by the RPC (v8)
                'image_stored', 'image_unavailable', 'extra_photos_stored',
            ]
            cache_cols_available = [col for col in cache_cols if col in nearby_locations_df.columns]
            cache_data = nearby_locations_df[cache_cols_available]
            candidates_to_cache = cache_data.to_dict("records")

            cache_service.set_cached_recommendations(
                request.latitude,
                request.longitude,
                candidates_to_cache,
            )

            logger.info("✅ Cached %d locations (user-agnostic, quality score pre-computed)", len(candidates_to_cache))

            # Use fresh cache to serve request (compute user-specific scores)
            scored_candidates = _rank_cached_candidates(
                candidates=candidates_to_cache,
                request_lat=request.latitude,
                request_lng=request.longitude,
                request_radius_km=request.radius_km,
                quality_weight=request.quality_weight,
                vibe_weight=request.vibe_weight,
                dietary_weight=request.dietary_weight,
                max_results=request.max_results,
                cuisine_filters=cuisine_filters,
                vibe_filters=vibe_filters,
                user_id=request.user_id,
                social_weight=request.social_weight,
                collaborative_weight=request.collaborative_weight,
                user_profile=user_profile,
            )

            logger.info(
                "📊 Computed user-specific scores and ranked: %d results",
                len(scored_candidates)
            )

            # Convert to DataFrame for consistent processing
            recs = pd.DataFrame(scored_candidates) if scored_candidates else pd.DataFrame()

            if not recs.empty:
                recs = recs.replace({pd.NA: None, float('nan'): None, np.nan: None})
        else:
            recs = pd.DataFrame()

    if recs.empty:
        logger.warning(
            "⚠️ NO RESULTS: No recommendations found for user=%s at (%.4f, %.4f) within %.1f km",
            request.user_id,
            request.latitude,
            request.longitude,
            request.radius_km,
        )
        logger.info(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        return ProximalResponse(
            user_id=request.user_id,
            center_lat=request.latitude,
            center_lon=request.longitude,
            radius_km=request.radius_km,
            total_results=0,
            recommendations=[],
            timestamp=datetime.utcnow().isoformat(),
        )

    # Extract weights_used from the first scored candidate (all share the same weights)
    weights_used = None
    if not recs.empty and "weights_used" in recs.columns:
        first_weights = recs.iloc[0].get("weights_used")
        if isinstance(first_weights, dict):
            weights_used = first_weights

    # Convert to response model
    recommendations = []
    for _, row in recs.iterrows():
        # Build friend attribution list
        friend_saves_raw = row.get("friend_saves")
        friend_saves = None
        if friend_saves_raw and isinstance(friend_saves_raw, list):
            friend_saves = [FriendSave(**fs) for fs in friend_saves_raw]

        recommendations.append(
            LocationRecommendation(
                location_id=int(row["location_id"]),
                name=row["name"],
                vicinity=_safe_optional_str(row.get("vicinity")),
                cuisine_primary=_safe_optional_str(row.get("cuisine_primary")),
                rating=float(row["rating"]) if pd.notna(row.get("rating")) else None,
                user_ratings_total=int(row["user_ratings_total"])
                if pd.notna(row.get("user_ratings_total"))
                else None,
                price_level=float(row["price_level"]) if pd.notna(row.get("price_level")) else None,
                distance_km=float(row["distance_km"]),
                vibe_score=float(row["vibe_score"]),
                dietary_score=float(row["dietary_score"]),
                quality_score=float(row["quality_score"]),
                social_score=float(row.get("social_score", 0.0)),
                collaborative_score=float(row.get("collaborative_score", 0.0)),
                final_score=float(row["final_score"]),
                rank=int(row["rank"]),
                friend_saves=friend_saves,
                **_photo_fields(row),
            )
        )

    # Log final response summary
    logger.info(
        "✅ REQUEST COMPLETE: Returning %d recommendations for user=%s at (%.4f, %.4f) within %.1f km",
        len(recommendations),
        request.user_id,
        request.latitude,
        request.longitude,
        request.radius_km,
    )
    if recommendations:
        top_rec = recommendations[0]
        logger.info(
            "🥇 Top result: %s (id=%d, distance=%.2f km, final_score=%.3f, social=%.3f)",
            top_rec.name,
            top_rec.location_id,
            top_rec.distance_km,
            top_rec.final_score,
            top_rec.social_score,
        )
    logger.info(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    return ProximalResponse(
        user_id=request.user_id,
        center_lat=request.latitude,
        center_lon=request.longitude,
        radius_km=request.radius_km,
        total_results=len(recommendations),
        recommendations=recommendations,
        weights_used=weights_used,
        timestamp=datetime.utcnow().isoformat(),
    )


async def _schedule_or_run_location_job(
    job_name: str,
    handler,
    *,
    process_synchronously: bool,
) -> None:
    if process_synchronously:
        await handler()
        return

    # Legacy in-process background queue (still used by older helpers in this module).
    await get_background_job_runner().enqueue(job_name, handler)


async def _upsert_video_insights_for_location(
    location_id: int,
    video_insights: Any,
) -> None:
    def _upsert() -> None:
        supabase = get_supabase_service()
        supabase.client.table("video_insights").upsert(
            {
                "source_video_url": video_insights.source_video_url,
                "location_id": location_id,
                "key_dishes": video_insights.key_dishes or [],
                "special_offers": video_insights.special_offers or [],
                "creator_notes": video_insights.creator_notes,
                "vibe_signals": video_insights.vibe_signals or {},
                "sentiment": video_insights.sentiment,
                "creator_handle": video_insights.creator_handle,
                "video_description": video_insights.video_description,
                "extraction_model": video_insights.extraction_model or "gpt-4o-mini",
            },
            on_conflict="source_video_url,location_id",
        ).execute()

    await asyncio.to_thread(_upsert)
    logger.info("Upserted video_insights for location %s before blend", location_id)


async def _run_emoji_generation(location_id: int, place_details: Dict[str, Any]) -> None:
    emoji = await asyncio.to_thread(add_location_emoji, place_details)
    if not emoji:
        return

    def _update_location_emoji() -> None:
        get_supabase_service().update_location(location_id, emoji=emoji)

    await asyncio.to_thread(_update_location_emoji)
    logger.info("Added emoji '%s' for location %s", emoji, location_id)


async def _run_photo_pipeline(
    location_id: int,
    photos_from_details: List[Dict[str, Any]],
) -> None:
    from pinit.api.services.proximal_service import download_photo

    bg_supabase = get_supabase_service()

    if not photos_from_details:
        logger.warning(
            "No photos returned by Places API for location %s — marking image_unavailable",
            location_id,
        )
        try:
            await asyncio.to_thread(bg_supabase.mark_location_image_unavailable, location_id)
        except Exception as exc:
            logger.error(
                "mark_location_image_unavailable failed for %s: %s",
                location_id,
                exc,
            )
        return

    primary = photos_from_details[0]
    primary_name = primary.get("name") if isinstance(primary, dict) else None
    if not primary_name:
        logger.warning(
            "First photo for location %s has no resource name — marking image_unavailable",
            location_id,
        )
        try:
            await asyncio.to_thread(bg_supabase.mark_location_image_unavailable, location_id)
        except Exception as exc:
            logger.error(
                "mark_location_image_unavailable failed for %s: %s",
                location_id,
                exc,
            )
        return

    primary_dl = await asyncio.to_thread(download_photo, primary_name, GOOGLE_PLACE_API_KEY)
    if primary_dl is None:
        logger.error(
            "Primary photo download failed for location %s (name=%s)",
            location_id,
            primary_name,
        )
        return

    primary_bytes, primary_ct = primary_dl
    try:
        await asyncio.to_thread(
            bg_supabase.upload_location_photo,
            location_id,
            primary_bytes,
            primary_ct,
            None,
        )
    except Exception as exc:
        logger.error(
            "Primary photo upload failed for location %s: %s",
            location_id,
            exc,
        )
        return

    try:
        await asyncio.to_thread(
            bg_supabase.mark_location_image_uploaded,
            location_id,
            photos_from_details,
            primary_name,
        )
    except Exception as exc:
        logger.error(
            "mark_location_image_uploaded failed for location %s: %s",
            location_id,
            exc,
        )
        return

    extras_uploaded = 0
    for idx, extra in enumerate(photos_from_details[1:10], start=1):
        extra_name = extra.get("name") if isinstance(extra, dict) else None
        if not extra_name:
            continue

        extra_dl = await asyncio.to_thread(download_photo, extra_name, GOOGLE_PLACE_API_KEY)
        if extra_dl is None:
            logger.warning(
                "Extra photo %d download failed for location %s",
                idx,
                location_id,
            )
            continue

        extra_bytes, extra_ct = extra_dl
        try:
            await asyncio.to_thread(
                bg_supabase.upload_location_photo,
                location_id,
                extra_bytes,
                extra_ct,
                idx,
            )
            extras_uploaded += 1
        except Exception as exc:
            logger.error(
                "Extra photo %d upload failed for location %s: %s",
                idx,
                location_id,
                exc,
            )

    if extras_uploaded > 0:
        try:
            await asyncio.to_thread(
                bg_supabase.mark_location_extra_photos_stored,
                location_id,
                extras_uploaded,
            )
        except Exception as exc:
            logger.error(
                "mark_location_extra_photos_stored failed for %s: %s",
                location_id,
                exc,
            )

    logger.info(
        "Photo pipeline complete for location %s: primary + %d extras",
        location_id,
        extras_uploaded,
    )


async def _run_menu_then_vibe(
    location_id: int,
    google_place_id: str,
    place_details: Dict[str, Any],
    *,
    blend_tiktok: bool,
) -> None:
    from pinit.api.services.menu_processing import process_menu_for_location
    from pinit.api.services.vibe_tagging import generate_vibe_tags_for_location

    website = place_details.get("website")
    if website:
        cuisine_value = place_details.get("cuisine_primary", "") or ""
        try:
            result = await process_menu_for_location(
                location_id=location_id,
                google_place_id=google_place_id,
                website=website,
                restaurant_name=place_details.get("name", ""),
                cuisine_hint=cuisine_value,
                detect_cuisine=not cuisine_value,
            )
            has_summary = bool(result and result.restaurant_description)
            num_runs = 5 if has_summary else 1
        except Exception as exc:
            logger.error("Menu processing failed for location %s: %s", location_id, exc)
            num_runs = 1
    else:
        num_runs = 1

    try:
        await generate_vibe_tags_for_location(
            location_id,
            num_runs=num_runs,
            blend_tiktok=blend_tiktok,
        )
    except Exception as exc:
        logger.error("Vibe tag generation failed for location %s: %s", location_id, exc)


async def _run_post_create_processing(
    location_id: int,
    google_place_id: str,
    place_details: Dict[str, Any],
    *,
    generate_emoji: bool,
    classify_photo: bool,
    source: str,
    process_synchronously: bool,
) -> None:
    blend_tiktok = source == "tiktok"

    if generate_emoji:
        async def _emoji_job(place_snapshot: Dict[str, Any] = place_details) -> None:
            await _run_emoji_generation(location_id, place_snapshot)

        await _schedule_or_run_location_job(
            f"location:{location_id}:emoji",
            _emoji_job,
            process_synchronously=process_synchronously,
        )

    if classify_photo:
        photos_from_details: List[Dict[str, Any]] = place_details.get("photos") or []

        async def _photo_job(photo_snapshot: List[Dict[str, Any]] = photos_from_details) -> None:
            await _run_photo_pipeline(location_id, photo_snapshot)

        await _schedule_or_run_location_job(
            f"location:{location_id}:photos",
            _photo_job,
            process_synchronously=process_synchronously,
        )

    async def _menu_vibe_job(place_snapshot: Dict[str, Any] = place_details) -> None:
        await _run_menu_then_vibe(
            location_id,
            google_place_id,
            place_snapshot,
            blend_tiktok=blend_tiktok,
        )

    await _schedule_or_run_location_job(
        f"location:{location_id}:menu-vibe",
        _menu_vibe_job,
        process_synchronously=process_synchronously,
    )


async def _run_new_location_enrichment(
    location_id: int,
    google_place_id: str,
    *,
    generate_emoji: bool,
    classify_photo: bool,
    source: str,
) -> None:
    place_details = await asyncio.to_thread(
        refresh_location_from_google_place_details,
        location_id,
        google_place_id,
    )
    if not place_details:
        logger.error(
            "Full details enrichment failed for location %s (%s)",
            location_id,
            google_place_id,
        )
        return

    await _run_post_create_processing(
        location_id,
        google_place_id,
        place_details,
        generate_emoji=generate_emoji,
        classify_photo=classify_photo,
        source=source,
        process_synchronously=False,
    )


async def _run_vibe_reprocessing(
    location_id: int,
    *,
    num_runs: int,
    blend_tiktok: bool,
) -> None:
    from pinit.api.services.vibe_tagging import generate_vibe_tags_for_location

    await generate_vibe_tags_for_location(
        location_id,
        num_runs=num_runs,
        blend_tiktok=blend_tiktok,
    )


@router.post("/locations/add", response_model=AddLocationResponse)
async def add_location_by_place_id(request: AddLocationRequest) -> AddLocationResponse:
    """
    Add or process a location by Google Place ID, with source-aware behaviour.

    Source-aware flow:
    - **New location** (not in DB): Default path fetches basic Google Place
      details, creates a row quickly, then queues the heavy enrichment work on
      background workers. If ``process_synchronously=true``, it instead runs the
      full enrichment pipeline inline before returning.
    - **Existing + source='tiktok'**: Always re-process vibe tagging with
      TikTok vibe blending (aggregates ALL video_insights rows for this
      location). Also updates reccomended_dishes with top 3 key_dishes.
      Skips Google Places API entirely.
    - **Existing + source='magic-search-open'**: Re-run full enrichment fan-out
      asynchronously so temporary magic-search cards become canonical records
      with details, emoji, photos, menu analysis, and vibe tags.
    - **Existing + source='in-app'/'instagram'**: Re-process vibe tagging
      ONLY if updated_vibe is False or NULL. Skips Google Places API.
    """
    supabase = get_supabase_service()
    source = (request.source or "in-app").lower().strip()
    process_synchronously = bool(request.process_synchronously)
    request_id = str(uuid4())

    existing_location = await asyncio.to_thread(
        supabase.get_location_by_google_place_id,
        request.google_place_id,
    )

    created_new = False
    if existing_location:
        location_id = existing_location["location_id"]
        already_existed = True
        updated_vibe = bool(existing_location.get("updated_vibe") or False)
        existing_name = existing_location.get("name")
    else:
        already_existed = False
        updated_vibe = False
        existing_name = None
        basic_place_details = await asyncio.to_thread(
            fetch_google_place_basic_details,
            request.google_place_id,
        )
        if not basic_place_details:
            raise HTTPException(
                status_code=404,
                detail=f"Could not fetch basic details for Google Place ID: {request.google_place_id}",
            )

        created_location = await asyncio.to_thread(
            create_location_from_place_details,
            basic_place_details,
        )
        if not created_location:
            raise HTTPException(
                status_code=500,
                detail=f"Could not create location for Google Place ID: {request.google_place_id}",
            )

        created_new = True
        location_id = created_location["location_id"]
        existing_name = basic_place_details.get("name")

    # Keep video_insights synchronous (no Pub/Sub task).
    if request.video_insights and source in ("tiktok", "instagram"):
        try:
            await _upsert_video_insights_for_location(location_id, request.video_insights)
        except Exception as exc:
            logger.warning("video_insights upsert failed for %s: %s", location_id, exc)

    pipeline_payload = PipelinePayload(
        task_type="pipeline",
        request_id=request_id,
        location_id=location_id,
        google_place_id=request.google_place_id,
        source=source,
        generate_emoji=bool(request.generate_emoji),
        classify_photo=bool(request.classify_photo),
        created_new=created_new,
    )

    if process_synchronously:
        await run_pipeline_inline(pipeline_payload)
        updated_location = await asyncio.to_thread(supabase.get_location, location_id) or {}
        return AddLocationResponse(
            success=True,
            message="Location processed synchronously",
            location_id=location_id,
            google_place_id=request.google_place_id,
            name=updated_location.get("name") or existing_name,
            tags_count=0,
            already_existed=already_existed,
            emoji=updated_location.get("emoji"),
        )

    # Async mode: publish to Pub/Sub (or fallback to in-process worker for local dev).
    pubsub_cfg = get_pubsub_config()
    dispatcher = PubSubDispatcher() if pubsub_cfg.enabled else InProcessDispatcher()
    logger.info(
        "locations/add: dispatch %s via %s (pubsub_enabled=%s project_id=%s topic=%s) (location_id=%s request_id=%s source=%s)",
        pipeline_payload.task_type,
        dispatcher.__class__.__name__,
        pubsub_cfg.enabled,
        pubsub_cfg.project_id,
        pubsub_cfg.topic,
        location_id,
        request_id,
        source,
    )
    await dispatcher.dispatch(pipeline_payload)

    if created_new:
        message = "Location successfully added; enrichment queued"
    elif source in ("tiktok", "instagram"):
        message = "Location exists; vibe reprocessing queued"
    elif not updated_vibe:
        message = "Location exists; vibe reprocessing queued"
    else:
        message = "Location already exists in database"

    return AddLocationResponse(
        success=True,
        message=message,
        location_id=location_id,
        google_place_id=request.google_place_id,
        name=existing_name,
        tags_count=None,
        already_existed=already_existed,
        emoji=None,
    )

    # Note: errors above are raised as HTTPException to surface to clients.


def _magic_search_negative_location_id(google_place_id: str) -> int:
    digest = hashlib.sha1(google_place_id.encode("utf-8")).hexdigest()
    return -int(digest[:12], 16)


def _magic_search_place_name(place: Dict[str, Any]) -> str:
    display = place.get("displayName")
    if isinstance(display, dict):
        text = str(display.get("text") or "").strip()
        if text:
            return text
    for key in ("name", "shortFormattedAddress", "formattedAddress"):
        value = str(place.get(key) or "").strip()
        if value:
            return value
    return "Unknown place"


def _magic_search_place_price_level(place: Dict[str, Any]) -> Optional[float]:
    value = place.get("priceLevel")
    if isinstance(value, (int, float)):
        return float(value)
    price_map = {
        "PRICE_LEVEL_FREE": 0.0,
        "PRICE_LEVEL_INEXPENSIVE": 1.0,
        "PRICE_LEVEL_MODERATE": 2.0,
        "PRICE_LEVEL_EXPENSIVE": 3.0,
        "PRICE_LEVEL_VERY_EXPENSIVE": 4.0,
    }
    return price_map.get(str(value or "").strip().upper())


def _magic_search_photo_reference(place: Dict[str, Any]) -> Optional[str]:
    photos = place.get("photos")
    if not isinstance(photos, list):
        return None
    for photo in photos:
        if not isinstance(photo, dict):
            continue
        name = str(photo.get("name") or "").strip()
        if name:
            return name
    return None


def _magic_search_google_baseline_score(rating: Any, user_ratings_total: Any) -> float:
    try:
        rating_score = max(0.0, min(float(rating or 0.0) / 5.0, 1.0))
    except (TypeError, ValueError):
        rating_score = 0.0
    try:
        review_count = max(0.0, float(user_ratings_total or 0.0))
    except (TypeError, ValueError):
        review_count = 0.0
    review_score = min(math.log1p(review_count) / math.log1p(1000.0), 1.0)
    if rating_score <= 0.0 and review_score <= 0.0:
        return 0.35
    return max(0.0, min((rating_score * 0.75) + (review_score * 0.25), 1.0))


def _magic_search_action_adjusted_score(candidate: Dict[str, Any]) -> float:
    base = float(candidate.get("final_score") or 0.0)
    saves = int(candidate.get("saves_count") or 0)
    been_to = int(candidate.get("been_to_count") or 0)
    likes = int(candidate.get("likes_count") or 0)
    shares = int(candidate.get("share_count") or 0)
    dislikes = int(candidate.get("dislikes_count") or 0)
    boost = min(0.25, saves * 0.015 + been_to * 0.010 + likes * 0.012 + shares * 0.010)
    penalty = min(0.35, dislikes * 0.040)
    return max(0.0, base + boost - penalty)


def _coerce_optional_bool(value: Any) -> Optional[bool]:
    """Coerce DB / Places-v1 booleans, ignoring NaN/None and unrecognised values."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        s = value.strip().lower()
        if s == "true":
            return True
        if s == "false":
            return False
    return None


def _coerce_opening_hours_text(value: Any) -> Optional[List[str]]:
    """Normalise opening_hours_text from a DB column or external candidate."""
    if value is None:
        return None
    if isinstance(value, list):
        cleaned = [str(item).strip() for item in value if str(item or "").strip()]
        return cleaned or None
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else None
    return None


def _magic_search_rich_fields(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Extract rich place fields (website, summaries, vibe booleans, …) for the response.

    Works uniformly for known DB rows (which already use snake_case column
    names) and for external Places-v1 candidates produced by
    ``_magic_search_external_candidate``.
    """
    return {
        "formatted_address": _safe_optional_str(candidate.get("formatted_address")),
        "business_status": _safe_optional_str(candidate.get("business_status")),
        "google_maps_uri": _safe_optional_str(candidate.get("google_maps_uri")),
        "website": _safe_optional_str(candidate.get("website")),
        "international_phone_number": _safe_optional_str(
            candidate.get("international_phone_number")
        ),
        "editorial_summary": _safe_optional_str(candidate.get("editorial_summary")),
        "review_summary": _safe_optional_str(candidate.get("review_summary")),
        "opening_hours_text": _coerce_opening_hours_text(
            candidate.get("opening_hours_text")
        ),
        "good_for_children": _coerce_optional_bool(candidate.get("good_for_children")),
        "good_for_groups": _coerce_optional_bool(candidate.get("good_for_groups")),
        "good_for_watching_sports": _coerce_optional_bool(
            candidate.get("good_for_watching_sports")
        ),
        "live_music": _coerce_optional_bool(candidate.get("live_music")),
        "outdoor_seating": _coerce_optional_bool(candidate.get("outdoor_seating")),
        "serves_beer": _coerce_optional_bool(candidate.get("serves_beer")),
        "serves_breakfast": _coerce_optional_bool(candidate.get("serves_breakfast")),
        "serves_brunch": _coerce_optional_bool(candidate.get("serves_brunch")),
        "serves_cocktails": _coerce_optional_bool(candidate.get("serves_cocktails")),
        "serves_coffee": _coerce_optional_bool(candidate.get("serves_coffee")),
        "serves_dessert": _coerce_optional_bool(candidate.get("serves_dessert")),
        "serves_dinner": _coerce_optional_bool(candidate.get("serves_dinner")),
        "serves_lunch": _coerce_optional_bool(candidate.get("serves_lunch")),
        "serves_vegetarian_food": _coerce_optional_bool(
            candidate.get("serves_vegetarian_food")
        ),
        "serves_wine": _coerce_optional_bool(candidate.get("serves_wine")),
    }


def _magic_search_summary_text(value: Any) -> Optional[str]:
    """Pull the human-readable text from a Places v1 summary object.

    Both ``editorialSummary`` and ``reviewSummary`` come back as nested
    LocalizedText / SummaryText objects. ``reviewSummary.text`` is itself a
    LocalizedText with its own ``text`` field, while ``editorialSummary``
    directly carries a ``text`` string.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        candidate = value.get("text")
        if isinstance(candidate, dict):
            candidate = candidate.get("text")
        if isinstance(candidate, str):
            text = candidate.strip()
            return text or None
    return None


def _magic_search_opening_hours_text(place: Dict[str, Any]) -> Optional[List[str]]:
    """Return the weekday-by-weekday opening hours strings, if available."""
    hours = place.get("currentOpeningHours") or place.get("regularOpeningHours") or {}
    if not isinstance(hours, dict):
        return None
    descriptions = hours.get("weekdayDescriptions")
    if not isinstance(descriptions, list):
        return None
    cleaned = [str(item).strip() for item in descriptions if str(item or "").strip()]
    return cleaned or None


def _fill_missing_magic_search_google_fields(
    candidate: Dict[str, Any],
    place: Optional[Dict[str, Any]],
) -> None:
    """Fill DB candidates with Google Text Search fields not stored locally yet."""
    if not place:
        return

    location = place.get("location") or {}
    types = place.get("types") or []
    opening = place.get("currentOpeningHours") or {}
    google_fields = {
        "formatted_address": place.get("formattedAddress"),
        "business_status": place.get("businessStatus"),
        "google_maps_uri": place.get("googleMapsUri"),
        "website": place.get("websiteUri"),
        "international_phone_number": place.get("internationalPhoneNumber"),
        "editorial_summary": _magic_search_summary_text(place.get("editorialSummary")),
        "review_summary": _magic_search_summary_text(place.get("reviewSummary")),
        "opening_hours_text": _magic_search_opening_hours_text(place),
        "photo_reference": _magic_search_photo_reference(place),
        "lat": location.get("latitude"),
        "lng": location.get("longitude"),
        "types": ",".join(types) if isinstance(types, list) else str(types),
        "open_now": opening.get("openNow"),
        "good_for_children": place.get("goodForChildren"),
        "good_for_groups": place.get("goodForGroups"),
        "good_for_watching_sports": place.get("goodForWatchingSports"),
        "live_music": place.get("liveMusic"),
        "outdoor_seating": place.get("outdoorSeating"),
        "serves_beer": place.get("servesBeer"),
        "serves_breakfast": place.get("servesBreakfast"),
        "serves_brunch": place.get("servesBrunch"),
        "serves_cocktails": place.get("servesCocktails"),
        "serves_coffee": place.get("servesCoffee"),
        "serves_dessert": place.get("servesDessert"),
        "serves_dinner": place.get("servesDinner"),
        "serves_lunch": place.get("servesLunch"),
        "serves_vegetarian_food": place.get("servesVegetarianFood"),
        "serves_wine": place.get("servesWine"),
    }

    for key, value in google_fields.items():
        if value in (None, "", []):
            continue
        if candidate.get(key) in (None, "", []):
            candidate[key] = value


def _magic_search_external_candidate(
    place: Dict[str, Any],
    *,
    request_lat: float,
    request_lng: float,
    search_position: int,
) -> Optional[Dict[str, Any]]:
    from pinit.core.recommendation.proximal_recommendation import haversine_distance

    gid = str(place.get("id") or "").strip()
    location = place.get("location") or {}
    lat = location.get("latitude")
    lng = location.get("longitude")
    if not gid or lat is None or lng is None:
        return None

    opening = place.get("currentOpeningHours") or {}
    open_now = opening.get("openNow")
    if open_now is False:
        return None

    lat_f = float(lat)
    lng_f = float(lng)
    distance_km = haversine_distance(request_lat, request_lng, lat_f, lng_f)
    rating = place.get("rating")
    reviews = place.get("userRatingCount")
    quality_score = _magic_search_google_baseline_score(rating, reviews)
    distance_score = max(0.0, 1.0 - min(distance_km / 5.0, 1.0))
    order_score = max(0.0, 1.0 - min(search_position, 20) / 20.0)
    final_score = (
        quality_score * 0.72
        + distance_score * 0.18
        + order_score * 0.10
    )
    types = place.get("types") or []

    return {
        "location_id": _magic_search_negative_location_id(gid),
        "google_place_id": gid,
        "is_known_location": False,
        "name": _magic_search_place_name(place),
        "vicinity": place.get("shortFormattedAddress") or place.get("formattedAddress"),
        "formatted_address": place.get("formattedAddress"),
        "business_status": place.get("businessStatus"),
        "google_maps_uri": place.get("googleMapsUri"),
        "website": place.get("websiteUri"),
        "international_phone_number": place.get("internationalPhoneNumber"),
        "editorial_summary": _magic_search_summary_text(place.get("editorialSummary")),
        "review_summary": _magic_search_summary_text(place.get("reviewSummary")),
        "opening_hours_text": _magic_search_opening_hours_text(place),
        "lat": lat_f,
        "lng": lng_f,
        "types": ",".join(types) if isinstance(types, list) else str(types),
        "open_now": open_now,
        "rating": float(rating) if rating is not None else None,
        "user_ratings_total": int(reviews) if reviews is not None else None,
        "price_level": _magic_search_place_price_level(place),
        "photo_reference": _magic_search_photo_reference(place),
        "distance_km": distance_km,
        "vibe_score": 0.0,
        "dietary_score": 0.0,
        "quality_score": quality_score,
        "social_score": 0.0,
        "collaborative_score": 0.0,
        "final_score": max(0.0, min(final_score, 1.0)),
        "good_for_children": place.get("goodForChildren"),
        "good_for_groups": place.get("goodForGroups"),
        "good_for_watching_sports": place.get("goodForWatchingSports"),
        "live_music": place.get("liveMusic"),
        "outdoor_seating": place.get("outdoorSeating"),
        "serves_beer": place.get("servesBeer"),
        "serves_breakfast": place.get("servesBreakfast"),
        "serves_brunch": place.get("servesBrunch"),
        "serves_cocktails": place.get("servesCocktails"),
        "serves_coffee": place.get("servesCoffee"),
        "serves_dessert": place.get("servesDessert"),
        "serves_dinner": place.get("servesDinner"),
        "serves_lunch": place.get("servesLunch"),
        "serves_vegetarian_food": place.get("servesVegetarianFood"),
        "serves_wine": place.get("servesWine"),
    }


def _get_or_parse_magic_intent(
    prompt: str,
    cache_service: Any,
) -> tuple[MagicIntent, bool]:
    normalised = normalise_prompt(prompt)
    cached = None
    if hasattr(cache_service, "get_magic_intent"):
        cached = cache_service.get_magic_intent(normalised)
    if cached:
        payload = cached.get("intent", cached)
        return MagicIntent.model_validate(payload), True

    intent = parse_magic_intent(prompt)
    if hasattr(cache_service, "set_magic_intent"):
        cache_service.set_magic_intent(
            normalised,
            {
                "intent": intent.model_dump(mode="json"),
                "cached_at": datetime.utcnow().isoformat(),
            },
        )
    logger.info("Parsed magic intent from prompt=%r: %s", prompt, intent.model_dump())
    return intent, False


async def _get_magic_user_profile(
    user_id: str,
    cache_service: Any,
    supabase: Any,
) -> tuple[Optional[Dict[str, Any]], bool]:
    if hasattr(cache_service, "get_user_profile"):
        cached = await asyncio.to_thread(cache_service.get_user_profile, user_id)
        if cached is not None:
            return cached, True

    if hasattr(cache_service, "get_or_build_user_profile"):
        profile = await asyncio.to_thread(
            cache_service.get_or_build_user_profile,
            user_id,
            supabase,
        )
        return profile, False

    profile = await asyncio.to_thread(supabase.get_user, user_id)
    if not profile:
        return None, False
    return {
        "user_id": user_id,
        "vibe_vector": profile.get("vibe_tag_affinity"),
        "dietary_vector": profile.get("dietary_requirement_tag_affinity"),
        "action_count": 0,
        "exists": True,
    }, False


@router.post("/locations/magic-search", response_model=MagicSearchResponse)
async def magic_search(request: MagicSearchRequest) -> MagicSearchResponse:
    """
    Search Google Places with a free-text prompt and rank known + external results.

    Flow:
    1. Text Search (New) API → raw place dicts with enterprise-tier fields
    2. Batch-load Supabase rows for place IDs already known to the database
    3. Build lightweight baseline candidates for unknown Google places
    4. Rank known places with app signals and unknown places with Google baseline
    5. Return MagicSearchResponse
    """
    started_at = perf_counter()
    radius_km = request.radius_km or 2.0
    debug = MagicSearchDebug()
    supabase = get_supabase_service()
    cache_service = get_cache_service()

    _FOOD_TYPES_SET = {
        "restaurant", "cafe", "bar", "food", "bakery",
        "meal_delivery", "meal_takeaway", "night_club",
    }

    intent_started = perf_counter()
    intent_task = asyncio.create_task(
        asyncio.to_thread(_get_or_parse_magic_intent, request.prompt, cache_service)
    )
    profile_task = asyncio.create_task(
        _get_magic_user_profile(request.user_id, cache_service, supabase)
    )

    try:
        intent, _cache_hit_intent = await intent_task
        debug.intent_parse_latency_ms = (perf_counter() - intent_started) * 1000.0
        debug.google_queries = intent.google_queries

        google_started = perf_counter()
        google_task = asyncio.create_task(
            get_or_fetch_google_candidates(
                intent,
                lat=request.latitude,
                lng=request.longitude,
                radius_km=radius_km,
                cache=cache_service,
            )
        )

        user_profile, profile_cache_hit = await profile_task
        debug.cache_hit_user_profile = profile_cache_hit
        if not user_profile:
            google_task.cancel()
            raise HTTPException(
                status_code=404,
                detail=f"User '{request.user_id}' not found",
            )

        google_result = await google_task
        debug.google_search_latency_ms = (perf_counter() - google_started) * 1000.0
        debug.cache_hit_google_search = google_result.cache_hit_google_search
        debug.total_google_calls = google_result.total_google_calls
        debug.total_candidates_before_dedupe = (
            google_result.total_candidates_before_dedupe
        )
        debug.total_candidates_after_dedupe = (
            google_result.total_candidates_after_dedupe
        )
        if google_result.google_queries:
            debug.google_queries = google_result.google_queries
        places = google_result.places
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    logger.info(
        "Magic search: async Google returned %d places for prompt=%r (%d API calls, cache_hit=%s)",
        len(places),
        request.prompt,
        debug.total_google_calls,
        debug.cache_hit_google_search,
    )

    logger.info(
        "Magic search: raw places returned (id: name): %s",
        [(place.get("id"), place.get("display_name")) for place in places],
    )

    if not places:
        debug.magic_search_latency_ms = (perf_counter() - started_at) * 1000.0
        logger.info("Magic search metrics: %s", debug.model_dump())
        return MagicSearchResponse(
            user_id=request.user_id,
            center_lat=request.latitude,
            center_lon=request.longitude,
            prompt=request.prompt,
            radius_km=radius_km,
            total_candidates=0,
            total_ranked=0,
            recommendations=[],
            sections=[],
            debug=debug,
            timestamp=datetime.utcnow().isoformat(),
        )

    hydration_started = perf_counter()

    # Step 2: Filter to food-type places and collect their google_place_ids
    food_place_ids: List[str] = []
    place_by_id: Dict[str, Dict[str, Any]] = {}
    for place in places:
        gid = place.get("id")
        if not gid:
            continue
        types_raw = place.get("types", [])
        # If the API returned types, enforce the food filter; if absent, allow through
        if types_raw and not any(t in _FOOD_TYPES_SET for t in types_raw):
            continue
        food_place_ids.append(gid)
        place_by_id[gid] = place

    # Batch-fetch all already-known locations in a single DB round-trip
    existing_by_gid = supabase.get_locations_by_google_place_ids(food_place_ids)

    location_ids_to_fetch: List[int] = []
    gid_by_location_id: Dict[int, str] = {}
    for gid in food_place_ids:
        if gid in existing_by_gid:
            lid = existing_by_gid[gid].get("location_id")
            if lid:
                lid_int = int(lid)
                location_ids_to_fetch.append(lid_int)
                gid_by_location_id[lid_int] = gid

    # Batch-fetch all full rows (quality_score, vibe_vector, etc.) in one query
    candidates: List[Dict[str, Any]] = supabase.get_locations_by_ids(location_ids_to_fetch)
    for candidate in candidates:
        loc_id = int(candidate.get("location_id"))
        gid = gid_by_location_id.get(loc_id)
        candidate.setdefault("google_place_id", gid)
        candidate["is_known_location"] = True
        _fill_missing_magic_search_google_fields(candidate, place_by_id.get(gid or ""))

    # Keep only places that are currently open (or whose status is unknown / not stored)
    # before_open_filter = len(candidates)
    # candidates = [c for c in candidates if c.get("open_now") is not False]
    # logger.info(
    #     "Magic search: %d food locations collected, %d open (filtered %d closed)",
    #     before_open_filter, len(candidates), before_open_filter - len(candidates),
    # )

    external_candidates: List[Dict[str, Any]] = []
    for index, gid in enumerate(food_place_ids):
        if gid in existing_by_gid:
            continue
        external = _magic_search_external_candidate(
            place_by_id[gid],
            request_lat=request.latitude,
            request_lng=request.longitude,
            search_position=index,
        )
        if external is not None:
            external_candidates.append(external)
    debug.place_hydration_latency_ms = (perf_counter() - hydration_started) * 1000.0

    logger.info(
        "Magic search: fetched the following candidates for ranking (location_id: name): %s",
        [(c["location_id"], c["name"]) for c in candidates],
    )
    # Step 4: Rank using the shared cache-style ranking (user-personalised scores)
    rerank_started = perf_counter()
    ranking_radius_km = max(radius_km, 50.0) if intent.location_rectangle else radius_km
    scored = _rank_cached_candidates(
        candidates=candidates,
        request_lat=request.latitude,
        request_lng=request.longitude,
        request_radius_km=ranking_radius_km,
        quality_weight=0.30,
        vibe_weight=0.25,
        dietary_weight=0.10,
        max_results=max(len(candidates), request.max_results or 20),
        user_id=request.user_id,
        social_weight=0.20,
        collaborative_weight=0.15,
        user_profile=user_profile,
    )
    for candidate in scored:
        candidate["final_score"] = _magic_search_action_adjusted_score(candidate)

    combined_scored = dedupe_magic_candidates(scored + external_candidates)
    combined_scored = rerank_magic_candidates(
        combined_scored,
        intent=intent,
        request_radius_km=ranking_radius_km,
    )
    combined_scored = combined_scored[: request.max_results or 20]
    for rank, candidate in enumerate(combined_scored, start=1):
        candidate["rank"] = rank
    debug.rerank_latency_ms = (perf_counter() - rerank_started) * 1000.0

    # Step 5: Build response
    recommendations = []
    for candidate in combined_scored:
        friend_saves_raw = candidate.get("friend_saves")
        friend_saves = None
        if friend_saves_raw and isinstance(friend_saves_raw, list):
            friend_saves = [FriendSave(**fs) for fs in friend_saves_raw]

        recommendations.append(
            MagicLocationRecommendation(
                location_id=int(candidate["location_id"]),
                google_place_id=_safe_optional_str(candidate.get("google_place_id")),
                is_known_location=bool(candidate.get("is_known_location", True)),
                name=candidate["name"],
                vicinity=_safe_optional_str(candidate.get("vicinity")),
                cuisine_primary=_safe_optional_str(candidate.get("cuisine_primary")),
                rating=float(candidate["rating"]) if candidate.get("rating") is not None else None,
                user_ratings_total=int(candidate["user_ratings_total"])
                if candidate.get("user_ratings_total") is not None
                else None,
                price_level=float(candidate["price_level"])
                if candidate.get("price_level") is not None
                else None,
                photo_reference=_safe_optional_str(candidate.get("photo_reference")),
                lat=float(candidate["lat"]) if candidate.get("lat") is not None else None,
                lng=float(candidate["lng"]) if candidate.get("lng") is not None else None,
                types=_safe_optional_str(candidate.get("types")),
                open_now=candidate.get("open_now"),
                distance_km=float(candidate["distance_km"]),
                vibe_score=float(candidate["vibe_score"]),
                dietary_score=float(candidate["dietary_score"]),
                quality_score=float(candidate["quality_score"]),
                social_score=float(candidate.get("social_score", 0.0)),
                collaborative_score=float(candidate.get("collaborative_score", 0.0)),
                final_score=float(candidate["final_score"]),
                rank=int(candidate["rank"]),
                source=list(candidate.get("source") or []),
                match_reasons=list(candidate.get("match_reasons") or []),
                intent_matches=dict(candidate.get("intent_matches") or {}),
                confidence=float(candidate.get("confidence") or candidate["final_score"]),
                friend_saves=friend_saves,
                **_photo_fields(candidate),
                **_magic_search_rich_fields(candidate),
            )
        )
    sections = [
        MagicSearchSection(**section)
        for section in build_magic_sections(recommendations)
    ]

    logger.info(
        "Magic search complete: prompt=%r, %d candidates → %d ranked",
        request.prompt, len(places), len(recommendations),
    )
    debug.total_ranked = len(recommendations)
    debug.magic_search_latency_ms = (perf_counter() - started_at) * 1000.0
    logger.info("Magic search metrics: %s", debug.model_dump())

    return MagicSearchResponse(
        user_id=request.user_id,
        center_lat=request.latitude,
        center_lon=request.longitude,
        prompt=request.prompt,
        radius_km=radius_km,
        total_candidates=len(places),
        total_ranked=len(recommendations),
        recommendations=recommendations,
        sections=sections,
        debug=debug,
        timestamp=datetime.utcnow().isoformat(),
    )


@router.post("/recommendations/bubble", response_model=BubbleResponse)
async def get_bubble_recommendations(request: BubbleRequest) -> BubbleResponse:
    """
    Group (bubble) recommendations.

    Reuses the same data path as /recommendations/proximal — same Redis cache,
    same slim PostGIS RPC, same diversification — and adds:
      * Activity-weighted average of users' effective vibe vectors as the
        single group vibe vector (one cosine per location).
      * Optional bias toward locations the group has added to bubble_locations
        for this bubble_id (intent_weight=0.5 by default).
      * Per-user vibe score breakdown returned per location.
      * Small additive boost (+0.1) on locations already in the bubble so the
        group's existing list surfaces near the top.

    Social and collaborative scores are intentionally disabled in bubble mode.
    """
    from pinit.core.recommendation.bubble_recommendation import (
        compute_user_activity_scores,
    )
    from pinit.core.recommendation.bubble_vibe import (
        build_effective_user_vectors,
        aggregate_group_vibe_vector,
    )

    supabase = get_supabase_service()

    logger.info(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    logger.info(
        "🫧  BUBBLE REQUEST: users=%s, bubble_id=%s, position=(%.4f, %.4f), "
        "radius=%.1f km, max_results=%d",
        request.user_ids,
        request.bubble_id,
        request.latitude,
        request.longitude,
        request.radius_km,
        request.max_results,
    )

    # ── Validate all users exist + fetch their slim vector rows in one pass.
    user_vectors: Dict[str, Optional[List[float]]] = {}
    user_dietary_vectors: Dict[str, Optional[List[float]]] = {}
    invalid_users: List[str] = []
    for user_id in request.user_ids:
        row = supabase.get_user_vectors(user_id)
        if not row:
            invalid_users.append(user_id)
            continue
        user_vectors[user_id] = row.get("vibe_tag_affinity")
        user_dietary_vectors[user_id] = row.get("dietary_requirement_tag_affinity")

    if invalid_users:
        raise HTTPException(
            status_code=404,
            detail=f"Users not found: {invalid_users}",
        )

    # ── Extract optional filters
    cuisine_filters = request.filters.cuisine if request.filters else None
    vibe_filters = request.filters.vibe if request.filters else None

    # ── Bubble intent: pull what the group has shortlisted, if a bubble was
    # supplied. Adds at most ~5-15 rows of egress.
    bubble_added_rows: List[Dict[str, Any]] = []
    if request.bubble_id:
        bubble_added_rows = supabase.get_bubble_added_locations(request.bubble_id)
        logger.info(
            "🫧  Bubble %s has %d added location(s) used for intent blending",
            request.bubble_id,
            len(bubble_added_rows),
        )

    bubble_location_ids: set = {
        row["location_id"]
        for row in bubble_added_rows
        if row.get("location_id") is not None
    }

    # ── Activity weights (per user_id, sums to 1.0)
    activity_weights = compute_user_activity_scores(request.user_ids)

    # ── Per-user effective vibe vectors (personal blended with bubble intent)
    effective_user_vibe_vecs = build_effective_user_vectors(
        user_vectors, bubble_added_rows
    )

    # ── Diagnostics: surface degenerate vectors early. A user whose personal
    # vibe_tag_affinity is None, empty, or uniform contributes zero signal to
    # centered cosine — their individual score will be 0 and the group score
    # will collapse to whatever non-uniform users remain.
    import numpy as _np_diag
    for uid in request.user_ids:
        personal = user_vectors.get(uid)
        if personal is None:
            logger.warning("🫧  User %s: vibe_tag_affinity is NULL in DB", uid)
            continue
        try:
            arr = _np_diag.asarray(personal, dtype=float)
        except Exception:
            logger.warning("🫧  User %s: vibe_tag_affinity unparseable (%r)", uid, personal)
            continue
        if arr.size == 0:
            logger.warning("🫧  User %s: vibe_tag_affinity is empty", uid)
            continue
        centered = arr - arr.mean()
        c_norm = float(_np_diag.linalg.norm(centered))
        logger.info(
            "🫧  User %s: vibe vector len=%d, mean=%.2f, centered_norm=%.3f, "
            "activity_weight=%.3f%s",
            uid, arr.size, float(arr.mean()), c_norm,
            activity_weights.get(uid, 0.0),
            " [UNIFORM — will contribute 0 to centered cosine]" if c_norm < 1e-9 else "",
        )

    # ── Single blended group vibe vector
    group_vibe_arr = aggregate_group_vibe_vector(
        effective_user_vibe_vecs, activity_weights
    )
    if group_vibe_arr is None:
        logger.warning(
            "🫧  No usable vibe vectors for any user in %s — bubble vibe "
            "scoring will return 0.0 for every candidate",
            request.user_ids,
        )
    else:
        centered_group = group_vibe_arr - group_vibe_arr.mean()
        logger.info(
            "🫧  Group vibe vector: len=%d, mean=%.2f, centered_norm=%.3f",
            group_vibe_arr.size,
            float(group_vibe_arr.mean()),
            float(_np_diag.linalg.norm(centered_group)),
        )

    # ── Cache → spatial RPC → rank, exactly like proximal
    cache_service = get_cache_service()
    cached_data = cache_service.get_cached_recommendations(
        request.latitude, request.longitude, request.radius_km
    )

    if cached_data is not None:
        candidates = cached_data.get("candidates", [])
        logger.info(
            "🫧  ✅ CACHE HIT: %d candidates from cache at (%.4f, %.4f)",
            len(candidates),
            cached_data.get("center_lat"),
            cached_data.get("center_lng"),
        )
    else:
        cache_radius_km = 15.0
        logger.info(
            "🫧  ❌ CACHE MISS at (%.4f, %.4f), querying %.1f km via PostGIS",
            request.latitude,
            request.longitude,
            cache_radius_km,
        )
        nearby_locations_data = supabase.get_locations_with_quality_scores(
            request.latitude,
            request.longitude,
            cache_radius_km,
            limit=1000,
        )
        if not nearby_locations_data:
            return BubbleResponse(
                user_ids=request.user_ids,
                group_size=len(request.user_ids),
                center_lat=request.latitude,
                center_lon=request.longitude,
                radius_km=request.radius_km,
                total_results=0,
                recommendations=[],
                optional_filters_applied=request.filters,
                locations_before_filtering=0,
                locations_after_filtering=0,
                timestamp=datetime.utcnow().isoformat(),
            )

        df = pd.DataFrame(nearby_locations_data)
        cache_cols = [
            'location_id', 'name', 'vicinity', 'cuisine_primary',
            'rating', 'user_ratings_total', 'price_level',
            'lat', 'lng', 'distance_km',
            'vibe_vector', 'dietary_requirement_vector',
            'quality_score',
            # Photo metadata surfaced by the RPC (v8)
            'image_stored', 'image_unavailable', 'extra_photos_stored',
        ]
        cache_cols_available = [c for c in cache_cols if c in df.columns]
        candidates = df[cache_cols_available].to_dict("records")

        cache_service.set_cached_recommendations(
            request.latitude, request.longitude, candidates
        )
        logger.info("🫧  ✅ Cached %d locations", len(candidates))

    locations_before = len(candidates)

    # ── Build group_context and call the shared ranker
    group_context: Dict[str, Any] = {
        "group_vibe_vec": group_vibe_arr.tolist() if group_vibe_arr is not None else None,
        "user_dietary_vectors": user_dietary_vectors,
        "user_effective_vibe_vecs": effective_user_vibe_vecs,
        "activity_weights": activity_weights,
        "bubble_location_ids": bubble_location_ids,
        "bubble_boost": 0.1,
    }

    scored = _rank_cached_candidates(
        candidates=candidates,
        request_lat=request.latitude,
        request_lng=request.longitude,
        request_radius_km=request.radius_km,
        quality_weight=request.quality_weight,
        vibe_weight=request.vibe_weight,
        dietary_weight=request.dietary_weight,
        max_results=request.max_results,
        cuisine_filters=cuisine_filters,
        vibe_filters=vibe_filters,
        user_id=None,
        social_weight=0.0,
        collaborative_weight=0.0,
        group_context=group_context,
    )

    locations_after = len(scored)

    if not scored:
        return BubbleResponse(
            user_ids=request.user_ids,
            group_size=len(request.user_ids),
            center_lat=request.latitude,
            center_lon=request.longitude,
            radius_km=request.radius_km,
            total_results=0,
            recommendations=[],
            optional_filters_applied=request.filters,
            locations_before_filtering=locations_before,
            locations_after_filtering=0,
            timestamp=datetime.utcnow().isoformat(),
        )

    # ── Serialise. Every numeric field is sanitised against NaN / inf / None
    # because individual numpy operations upstream can silently produce NaN
    # (e.g. when a user has a degenerate all-zero vibe vector) and json.dumps
    # refuses to encode it.
    import math as _math

    def _safe_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
        if value is None:
            return default
        try:
            f = float(value)
        except (TypeError, ValueError):
            return default
        if _math.isnan(f) or _math.isinf(f):
            return default
        return f

    def _safe_optional_float(value: Any) -> Optional[float]:
        return _safe_float(value, default=None)

    def _safe_optional_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        if _math.isnan(f) or _math.isinf(f):
            return None
        return int(f)

    recommendations = []
    for c in scored:
        per_user_vibes = [
            UserVibeScore(
                user_id=ivs["user_id"],
                vibe_score=_safe_float(ivs.get("vibe_score"), default=0.0),
            )
            for ivs in (c.get("individual_vibe_scores") or [])
        ]

        recommendations.append(
            BubbleLocationRecommendation(
                location_id=int(c["location_id"]),
                name=c["name"],
                vicinity=_safe_optional_str(c.get("vicinity")),
                cuisine_primary=_safe_optional_str(c.get("cuisine_primary")),
                rating=_safe_optional_float(c.get("rating")),
                user_ratings_total=_safe_optional_int(c.get("user_ratings_total")),
                price_level=_safe_optional_float(c.get("price_level")),
                distance_km=_safe_float(c.get("distance_km"), default=0.0),
                group_vibe_score=_safe_float(c.get("vibe_score"), default=0.0),
                group_dietary_score=_safe_float(c.get("dietary_score"), default=0.0),
                quality_score=_safe_float(c.get("quality_score"), default=0.0),
                final_score=_safe_float(c.get("final_score"), default=0.0),
                rank=int(c["rank"]),
                individual_vibe_scores=per_user_vibes,
                is_in_bubble=bool(c.get("is_in_bubble", False)),
                **_photo_fields(c),
            )
        )

    logger.info(
        "🫧  ✅ BUBBLE COMPLETE: %d recommendations (in_bubble=%d)",
        len(recommendations),
        sum(1 for r in recommendations if r.is_in_bubble),
    )
    logger.info(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    return BubbleResponse(
        user_ids=request.user_ids,
        group_size=len(request.user_ids),
        center_lat=request.latitude,
        center_lon=request.longitude,
        radius_km=request.radius_km,
        total_results=len(recommendations),
        recommendations=recommendations,
        optional_filters_applied=request.filters,
        locations_before_filtering=locations_before,
        locations_after_filtering=locations_after,
        timestamp=datetime.utcnow().isoformat(),
    )
