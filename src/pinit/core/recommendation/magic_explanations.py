from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence

from pinit.api.schemas_magic import MagicIntent


def _score(scores: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(scores.get(key, default) or 0.0)
    except (TypeError, ValueError):
        return default


def _candidate_value(candidate: Any, key: str, default: Any = None) -> Any:
    if isinstance(candidate, Mapping):
        return candidate.get(key, default)
    return getattr(candidate, key, default)


def _budget_cuisine_reason(intent: MagicIntent, scores: Mapping[str, Any]) -> str | None:
    if not intent.budget or not intent.cuisines:
        return None
    if _score(scores, "budget") < 0.75 or _score(scores, "cuisine") < 0.75:
        return None
    cuisine = intent.cuisines[0].replace("_", " ").title()
    return f"Matches {intent.budget} {cuisine}"


def _generic_query_reason(intent: MagicIntent, scores: Mapping[str, Any]) -> str | None:
    if _score(scores, "overall") < 0.7:
        return None
    if intent.cuisines:
        return f"Strong {intent.cuisines[0].replace('_', ' ')} match"
    if intent.occasion:
        return f"Fits {intent.occasion.replace('_', ' ')}"
    if intent.vibe_tags:
        return "Matches the vibe you asked for"
    return None


def _append_unique(reasons: List[str], reason: str | None) -> None:
    if reason and reason not in reasons:
        reasons.append(reason)


def build_match_reasons(
    candidate: Mapping[str, Any],
    intent: MagicIntent,
    score_breakdown: Mapping[str, Any],
) -> List[str]:
    """Build deterministic user-facing reasons from score components."""
    reasons: List[str] = []

    _append_unique(reasons, _budget_cuisine_reason(intent, score_breakdown))
    _append_unique(reasons, _generic_query_reason(intent, score_breakdown))

    if intent.occasion == "group" and _score(score_breakdown, "occasion") >= 0.75:
        _append_unique(reasons, "Good for groups")

    if _score(score_breakdown, "vibe") >= 0.75 or float(candidate.get("vibe_score") or 0.0) >= 0.75:
        _append_unique(reasons, "Strong match with your usual vibe")

    if _score(score_breakdown, "availability") >= 0.95 or candidate.get("open_now") is True:
        _append_unique(reasons, "Open now")

    if float(candidate.get("social_score") or 0.0) > 0 or candidate.get("friend_saves"):
        _append_unique(reasons, "People you trust have saved this")

    if _score(score_breakdown, "novelty") >= 0.75:
        _append_unique(reasons, "Hidden gem signal")

    if _score(score_breakdown, "google_quality") >= 0.8:
        _append_unique(reasons, "Highly rated on Google")

    if _score(score_breakdown, "agentic_web") >= 0.04:
        _append_unique(reasons, "Suggested by Magic Search AI")

    if not reasons:
        _append_unique(reasons, "Good fit for this search")

    return reasons[:5]


def _slice_top(recommendations: Sequence[Any], limit: int) -> List[Any]:
    return list(recommendations[:limit])


def build_magic_sections(recommendations: Sequence[Any]) -> List[Dict[str, Any]]:
    """Group ranked Magic Search results into curated response sections."""
    if not recommendations:
        return []

    sections: List[Dict[str, Any]] = [
        {
            "title": "Best matches",
            "subtitle": "Highest overall Magic Search score",
            "recommendations": _slice_top(recommendations, 3),
        }
    ]

    friend_backed = [
        item
        for item in recommendations
        if float(_candidate_value(item, "social_score", 0.0) or 0.0) > 0
        or _candidate_value(item, "friend_saves")
    ]
    if friend_backed:
        sections.append(
            {
                "title": "Friend-backed picks",
                "subtitle": "Places with trusted social signal",
                "recommendations": _slice_top(friend_backed, 3),
            }
        )

    hidden_gems = [
        item
        for item in recommendations
        if _score(_candidate_value(item, "intent_matches", {}) or {}, "novelty") >= 0.7
    ]
    if hidden_gems:
        sections.append(
            {
                "title": "Hidden gems",
                "subtitle": "Strong ratings without obvious touristy mass appeal",
                "recommendations": _slice_top(hidden_gems, 3),
            }
        )

    safe_nearby = [
        item
        for item in recommendations
        if _score(_candidate_value(item, "intent_matches", {}) or {}, "google_quality") >= 0.75
        and _score(_candidate_value(item, "intent_matches", {}) or {}, "availability") >= 0.6
    ]
    if safe_nearby:
        sections.append(
            {
                "title": "Reliable nearby options",
                "subtitle": "High Google quality with practical availability",
                "recommendations": _slice_top(safe_nearby, 3),
            }
        )

    return sections[:4]
