"""
Vector utility functions for recommendation scoring.

Provides centered cosine similarity and dot product operations for
comparing user and location vector embeddings.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)


def centered_cosine_similarity(vec1: List[int], vec2: List[int]) -> float:
    """
    Compute centered cosine similarity (Pearson correlation) between two int4[] vectors.

    This is critical for handling cold start when user vectors are uniform.
    Regular cosine similarity breaks down when vectors are uniform (all same value)
    because it rewards "jack of all trades" restaurants over specialized ones.

    Centered cosine:
    1. Subtracts mean from each vector (centers them)
    2. Computes cosine similarity on centered vectors
    3. Returns correlation in [-1, 1] range, normalized to [0, 1]

    Returns: float in [0, 1]
    Edge cases:
    - None/empty vectors → 0.0
    - Uniform vectors (e.g., [50,50,50]) → centered to [0,0,0], similarity = 0.0
    - Dimension mismatch → pad shorter with zeros before centering

    Math:
    centered_cosine(u, r) = (u' · r') / (‖u'‖ · ‖r'‖)
    where u' = u - mean(u), r' = r - mean(r)

    This measures shape similarity, not magnitude similarity.

    Args:
        vec1: First vector (user affinity vector)
        vec2: Second vector (location vector)

    Returns:
        Similarity score in [0, 1] range
    """
    # Check for None or empty vectors
    if not vec1 or not vec2:
        return 0.0

    # Convert to numpy arrays
    try:
        arr1 = np.array(vec1, dtype=float)
        arr2 = np.array(vec2, dtype=float)
    except (TypeError, ValueError):
        logger.warning("Invalid vector format, returning 0.0")
        return 0.0

    # Handle dimension mismatch - pad shorter with zeros
    if len(arr1) != len(arr2):
        max_len = max(len(arr1), len(arr2))
        if len(arr1) < max_len:
            arr1 = np.pad(arr1, (0, max_len - len(arr1)), mode='constant')
        else:
            arr2 = np.pad(arr2, (0, max_len - len(arr2)), mode='constant')

    # Center both vectors (subtract mean)
    mean1 = np.mean(arr1)
    mean2 = np.mean(arr2)
    centered1 = arr1 - mean1
    centered2 = arr2 - mean2

    # Compute norms
    norm1 = np.linalg.norm(centered1)
    norm2 = np.linalg.norm(centered2)

    # Handle zero norm (uniform vectors) - no preference signal
    if norm1 == 0 or norm2 == 0:
        return 0.0

    # Compute centered cosine similarity
    similarity = np.dot(centered1, centered2) / (norm1 * norm2)

    # Normalize from [-1, 1] to [0, 1]
    normalized = (similarity + 1.0) / 2.0

    # Clip to ensure [0, 1] range (handle floating point errors)
    return float(np.clip(normalized, 0.0, 1.0))


def dot_product(vec1: List[int], vec2: List[int]) -> float:
    """
    Compute normalized dot product between two int4[] vectors.

    Used for dietary requirement matching where we want absolute
    alignment (not relative preference shapes like in vibe matching).

    Returns: float in [0, 1] range
    Edge cases: None/empty vectors → 0.0, dimension mismatch → pad with zeros

    Args:
        vec1: First vector (user dietary affinity vector)
        vec2: Second vector (location dietary vector)

    Returns:
        Normalized dot product in [0, 1] range
    """
    # Check for None or empty vectors
    if not vec1 or not vec2:
        return 0.0

    # Convert to numpy arrays
    try:
        arr1 = np.array(vec1, dtype=float)
        arr2 = np.array(vec2, dtype=float)
    except (TypeError, ValueError):
        logger.warning("Invalid vector format, returning 0.0")
        return 0.0

    # Handle dimension mismatch - pad shorter with zeros
    if len(arr1) != len(arr2):
        max_len = max(len(arr1), len(arr2))
        if len(arr1) < max_len:
            arr1 = np.pad(arr1, (0, max_len - len(arr1)), mode='constant')
        else:
            arr2 = np.pad(arr2, (0, max_len - len(arr2)), mode='constant')

    # Compute dot product
    dot_prod = np.dot(arr1, arr2)

    # Normalize to [0, 1] by dividing by max possible value
    # Max value occurs when both vectors are at maximum (100)
    max_possible = len(arr1) * 100 * 100  # Assuming int4[] values are 0-100 scale

    if max_possible == 0:
        return 0.0

    normalized = dot_prod / max_possible

    # Clip to ensure [0, 1] range
    return float(np.clip(normalized, 0.0, 1.0))
