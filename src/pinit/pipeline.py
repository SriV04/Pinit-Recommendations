from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict

from .config import PipelineConfig, PipelinePaths, ReviewTagConfig
from .recommendation import build_recommendations
from .tag_taxonomy import tag_dataframe
from .tagging import build_location_tags, load_locations, load_reviews
from .user_profiles import (
    build_user_tag_affinities,
    ensure_user_actions,
)


def run_pipeline(config: PipelineConfig) -> Dict[str, Path]:
    paths = config.paths
    output_dir = paths.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    locations = load_locations(paths)
    place_lookup = locations.set_index("google_place_id")["location_id"].to_dict()
    reviews = load_reviews(paths, place_lookup)
    tags = tag_dataframe()
    location_tags = build_location_tags(locations, reviews, config.review_tagging)

    user_actions, synthetic = ensure_user_actions(
        paths, locations, location_tags, allow_synthetic=config.synthetic_users
    )
    user_tags, user_history = build_user_tag_affinities(user_actions, location_tags, locations)

    recommendations = build_recommendations(
        locations,
        user_tags,
        location_tags,
        user_history,
        user_actions,
        config,
    )

    outputs = {
        "locations": output_dir / "locations.csv",
        "tags": output_dir / "tags.csv",
        "location_tags": output_dir / "location_tags.csv",
        "user_tags": output_dir / "user_tag_affinities.csv",
        "user_history": output_dir / "user_history.csv",
        "user_recommendations": output_dir / "user_recommendations.csv",
        "metadata": output_dir / "metadata.json",
    }

    locations.to_csv(outputs["locations"], index=False)
    tags.to_csv(outputs["tags"], index=False)
    location_tags.to_csv(outputs["location_tags"], index=False)
    user_tags.to_csv(outputs["user_tags"], index=False)
    user_history.to_csv(outputs["user_history"], index=False)
    recommendations.to_csv(outputs["user_recommendations"], index=False)

    metadata = {
        "city": paths.city_name,
        "n_locations": int(len(locations)),
        "n_tags": int(len(tags)),
        "n_location_tags": int(len(location_tags)),
        "n_users": int(user_tags["user_id"].nunique()) if not user_tags.empty else 0,
        "n_recommendations": int(len(recommendations)),
        "synthetic_user_actions": synthetic,
    }
    outputs["metadata"].write_text(json.dumps(metadata, indent=2))
    return outputs


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the local Pinit recommendation pipeline (no Supabase required)."
    )
    parser.add_argument("--data-dir", type=str, default="data/raw", help="Directory containing CSV inputs.")
    parser.add_argument("--city-name", type=str, default="london", help="City slug to use when resolving CSV names.")
    parser.add_argument("--output-dir", type=str, default="output/pinit", help="Where to place generated tables.")
    parser.add_argument("--user-actions", type=str, default="", help="Optional CSV with historical user actions.")
    parser.add_argument("--top-k", type=int, default=30, help="How many recs to keep per user.")
    parser.add_argument("--no-synthesize-users", action="store_true", help="Disable synthetic user generation fallback.")
    parser.add_argument("--review-min-authors", type=int, default=2, help="Min distinct authors for review-derived tags.")
    parser.add_argument("--review-min-mentions", type=int, default=3, help="Min mentions for review-derived tags.")
    return parser.parse_args()


def main():
    args = _parse_args()
    paths = PipelinePaths(
        data_dir=Path(args.data_dir),
        city_name=args.city_name,
        output_dir=Path(args.output_dir),
        user_actions_csv=Path(args.user_actions) if args.user_actions else None,
    )
    review_cfg = ReviewTagConfig(
        min_unique_authors=args.review_min_authors,
        min_mentions=args.review_min_mentions,
    )
    config = PipelineConfig(
        paths=paths,
        review_tagging=review_cfg,
        synthetic_users=not args.no_synthesize_users,
        top_k_per_user=args.top_k,
    )
    run_pipeline(config)
    print(f"[pinit] Recommendation artifacts saved to: {paths.output_dir.resolve()}")


if __name__ == "__main__":
    main()
