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


# Section subtitles, keyed by header title. Display order is fixed below.
_MAGIC_SECTION_SUBTITLES: Dict[str, str] = {
    "Best matches": "Highest overall Magic Search score",
    "Friend-backed picks": "Places with trusted social signal",
    "Hidden gems": "Strong ratings without obvious touristy mass appeal",
    "Reliable nearby options": "High Google quality with practical availability",
    "More places": "More spots worth a look",
}
_MAGIC_SECTION_ORDER: List[str] = list(_MAGIC_SECTION_SUBTITLES.keys())


def _is_friend_backed(item: Any) -> bool:
    return (
        float(_candidate_value(item, "social_score", 0.0) or 0.0) > 0
        or bool(_candidate_value(item, "friend_saves"))
    )


def _is_hidden_gem(item: Any) -> bool:
    scores = _candidate_value(item, "intent_matches", {}) or {}
    return _score(scores, "novelty") >= 0.7


def _is_reliable_nearby(item: Any) -> bool:
    scores = _candidate_value(item, "intent_matches", {}) or {}
    return _score(scores, "google_quality") >= 0.75 and _score(scores, "availability") >= 0.6


def _assign_magic_section(item: Any, index: int) -> str:
    """Pick the single section header for a ranked recommendation.

    The top 3 are the headline "Best matches"; the rest fall into the first
    themed section they qualify for, defaulting to "More places".
    """
    if index < 3:
        return "Best matches"
    if _is_friend_backed(item):
        return "Friend-backed picks"
    if _is_hidden_gem(item):
        return "Hidden gems"
    if _is_reliable_nearby(item):
        return "Reliable nearby options"
    return "More places"


def build_magic_sections(recommendations: Sequence[Any]) -> List[Dict[str, Any]]:
    """Assign each recommendation a single ``section`` header and return the
    ordered, header-only section list.

    Mutates each recommendation in place (sets ``section``) rather than
    relisting it, so a place is serialised exactly once in the response.
    """
    if not recommendations:
        return []

    used: set[str] = set()
    for index, item in enumerate(recommendations):
        title = _assign_magic_section(item, index)
        if isinstance(item, Mapping):
            item["section"] = title
        else:
            setattr(item, "section", title)
        used.add(title)

    return [
        {"title": title, "subtitle": _MAGIC_SECTION_SUBTITLES[title]}
        for title in _MAGIC_SECTION_ORDER
        if title in used
    ]
