from __future__ import annotations

import logging
from typing import Optional, Sequence, Tuple

import numpy as np
import pandas as pd

try:
    from sklearn.compose import ColumnTransformer
    from sklearn.compose import TransformedTargetRegressor
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder

    _SKLEARN_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    ColumnTransformer = None  # type: ignore
    HistGradientBoostingRegressor = None  # type: ignore
    Pipeline = None  # type: ignore
    SimpleImputer = None  # type: ignore
    OneHotEncoder = None  # type: ignore
    TransformedTargetRegressor = None  # type: ignore
    _SKLEARN_AVAILABLE = False

LOGGER = logging.getLogger(__name__)

IMPORTANT_TYPES: Sequence[str] = (
    "restaurant",
    "cafe",
    "bar",
    "meal_takeaway",
    "meal_delivery",
    "bakery",
    "night_club",
    "store",
)

MIN_MODEL_ROWS = 50
DEFAULT_MIN_REVIEWS = 40


def _min_max_scale(values: pd.Series) -> pd.Series:
    v_min = values.min()
    v_max = values.max()
    if pd.isna(v_min) or pd.isna(v_max) or np.isclose(v_min, v_max):
        return pd.Series(np.zeros(len(values)), index=values.index)
    return (values - v_min) / (v_max - v_min)


def _types_to_set(value) -> set[str]:
    if isinstance(value, set):
        return {str(v).strip().lower() for v in value if str(v).strip()}
    if isinstance(value, (list, tuple)):
        return {str(v).strip().lower() for v in value if str(v).strip()}
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return set()
    return {t.strip().lower() for t in str(value).split(",") if t.strip()}


def _prepare_model_frame(locations: pd.DataFrame) -> pd.DataFrame:
    df = locations.copy()
    df["rating"] = pd.to_numeric(df.get("rating"), errors="coerce")
    df["user_ratings_total"] = pd.to_numeric(
        df.get("user_ratings_total"), errors="coerce"
    ).fillna(0)
    df["log_reviews"] = np.log1p(df["user_ratings_total"])
    df["price_level"] = pd.to_numeric(df.get("price_level"), errors="coerce")
    cuisine = df.get("cuisine_primary", "unknown")
    df["cuisine_model"] = (
        cuisine.fillna("unknown").astype(str).str.lower()
    )
    grid = df.get("grid_id")
    if grid is None:
        df["grid_id_model"] = "unknown"
    else:
        df["grid_id_model"] = grid.fillna("unknown").astype(str)
    business_status = df.get("business_status")
    if business_status is None:
        df["business_status_model"] = "unknown"
    else:
        df["business_status_model"] = (
            business_status.fillna("unknown").astype(str).str.lower()
        )
    types_src = df.get("types_list")
    if types_src is None:
        types_src = df.get("types")
    if types_src is None:
        type_sets = pd.Series([set()] * len(df), index=df.index)
    else:
        type_sets = types_src.apply(_types_to_set)
    for t in IMPORTANT_TYPES:
        df[f"type_{t}"] = type_sets.apply(lambda st, target=t: int(target in st))
    df = df[df["rating"].notna()]
    return df


def _rating_transform(y_in: np.ndarray) -> np.ndarray:
    y_clamped = np.clip(y_in, 1.01, 4.99)
    y_norm = (y_clamped - 1) / 4.0
    return np.log(y_norm / (1 - y_norm))


def _rating_inverse_transform(z: np.ndarray) -> np.ndarray:
    y_norm = 1 / (1 + np.exp(-z))
    return y_norm * 4.0 + 1.0


def _fit_hype_model(
    model_df: pd.DataFrame,
    min_reviews: int,
) -> Optional[Tuple[pd.Series, pd.Series, pd.Series]]:
    if not _SKLEARN_AVAILABLE:
        return None
    if len(model_df) < MIN_MODEL_ROWS:
        return None
    numeric_features = ["log_reviews", "price_level"] + [f"type_{t}" for t in IMPORTANT_TYPES]
    categorical_features = ["cuisine_model", "grid_id_model", "business_status_model"]
    numeric_features = [c for c in numeric_features if c in model_df.columns]
    categorical_features = [c for c in categorical_features if c in model_df.columns]
    if not numeric_features and not categorical_features:
        return None
    transformers = []
    if numeric_features:
        transformers.append(
            (
                "num",
                Pipeline(steps=[("imputer", SimpleImputer(strategy="median"))]),
                numeric_features,
            )
        )
    if categorical_features:
        transformers.append(
            (
                "cat",
                Pipeline(
                    steps=[
                        ("imputer", SimpleImputer(strategy="most_frequent")),
                        ("onehot", OneHotEncoder(handle_unknown="ignore")),
                    ]
                ),
                categorical_features,
            )
        )
    preprocess = ColumnTransformer(transformers=transformers, sparse_threshold=0)
    gbr = HistGradientBoostingRegressor(
        max_depth=6,
        learning_rate=0.05,
        max_iter=300,
        random_state=42,
    )
    model = TransformedTargetRegressor(
        regressor=gbr,
        func=_rating_transform,
        inverse_func=_rating_inverse_transform,
    )
    pipe = Pipeline(steps=[("prep", preprocess), ("model", model)])
    X = model_df[numeric_features + categorical_features]
    y = model_df["rating"]
    try:
        pipe.fit(X, y)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.warning("Hidden gem model failed to fit: %s", exc)
        return None
    expected_rating = pd.Series(pipe.predict(X), index=model_df.index)
    hype_residual = y - expected_rating
    review_counts = model_df["user_ratings_total"]
    review_weight = _min_max_scale(np.log1p(review_counts))
    signal = hype_residual.clip(lower=0)
    signal = signal * (0.35 + 0.65 * review_weight)
    signal = signal.where(review_counts >= min_reviews, 0.0)
    return expected_rating, hype_residual, signal


def _legacy_hidden_gem(df: pd.DataFrame) -> pd.Series:
    if "residual_popularity" not in df.columns:
        return pd.Series(np.zeros(len(df)), index=df.index)
    legacy = df["residual_popularity"].fillna(0).clip(upper=0).abs()
    return _min_max_scale(legacy)


def add_hidden_gem_scores(
    locations: pd.DataFrame, min_reviews: int = DEFAULT_MIN_REVIEWS
) -> pd.DataFrame:
    df = locations.copy()
    model_df = _prepare_model_frame(df)
    expected_rating = pd.Series(np.nan, index=df.index)
    hype_residual = pd.Series(np.nan, index=df.index)
    result = _fit_hype_model(model_df, min_reviews)
    if result is None:
        df["hidden_gem_score"] = _legacy_hidden_gem(df)
        df["hidden_gem_source"] = "popularity_residual"
        df["expected_rating"] = expected_rating
        df["hype_residual"] = hype_residual
        return df
    exp, resid, signal = result
    expected_rating.loc[exp.index] = exp
    hype_residual.loc[resid.index] = resid
    df["expected_rating"] = expected_rating
    df["hype_residual"] = hype_residual
    signal_full = pd.Series(0.0, index=df.index)
    signal_full.loc[signal.index] = signal
    if float(signal_full.max()) <= 0:
        df["hidden_gem_score"] = _legacy_hidden_gem(df)
        df["hidden_gem_source"] = "popularity_residual"
    else:
        df["hidden_gem_signal"] = signal_full
        df["hidden_gem_score"] = _min_max_scale(signal_full.clip(lower=0))
        df["hidden_gem_source"] = "rating_model"
    return df
