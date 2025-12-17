"""
Local implementation of the Pinit recommendation plan.

The package provides utilities for:
    * building a canonical location inventory from the scraped CSVs,
    * deriving a tag taxonomy and per-location tag scores,
    * modeling user taste profiles from interaction logs (or synthetic samples),
    * generating personalized recommendation lists with hidden-gem logic.

Everything runs against CSVs/Parquet files so it can be executed without a
Supabase connection.
"""

from .pipeline import run_pipeline

__all__ = ["run_pipeline"]
