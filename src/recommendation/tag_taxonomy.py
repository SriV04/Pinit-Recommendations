"""Load and organize tags from Supabase by category."""

from typing import Any, Dict, List
import pandas as pd
from supabase_client.supabase_service import get_supabase_service


def get_tags_dataframe(limit: int = 1000) -> pd.DataFrame:
    """
    Fetch all tags from Supabase and return as a DataFrame.
    
    Returns:
        DataFrame with columns: tag_id, text, tag_type, prompt_description, Colour
    """
    db = get_supabase_service()
    tags = db.get_all_tags(limit=limit)
    
    if not tags:
        return pd.DataFrame(columns=["tag_id", "text", "tag_type", "prompt_description", "Colour"])
    
    return pd.DataFrame(tags)


def get_tags_by_category(limit: int = 1000) -> Dict[str, pd.DataFrame]:
    """
    Fetch all tags from Supabase organized by tag_type.
    
    Returns:
        Dictionary mapping tag_type to DataFrame of tags in that category
    """
    df = get_tags_dataframe(limit=limit)
    
    if df.empty:
        return {}
    
    categories = {}
    for tag_type in df["tag_type"].unique():
        categories[tag_type] = df[df["tag_type"] == tag_type].reset_index(drop=True)
    
    return categories


def get_tag_lookup() -> Dict[str, Dict[str, Any]]:
    """
    Create a lookup dictionary mapping tag text to full tag record.
    
    Returns:
        Dictionary with tag text as key and full tag dict as value
    """
    db = get_supabase_service()
    tags = db.get_all_tags(limit=1000)
    
    return {tag["text"]: tag for tag in tags}


def get_tag_id_lookup() -> Dict[str, str]:
    """
    Create a lookup dictionary mapping tag text to tag_id.
    
    Returns:
        Dictionary with tag text as key and tag_id (UUID) as value
    """
    db = get_supabase_service()
    tags = db.get_all_tags(limit=1000)
    
    return {tag["text"]: tag["tag_id"] for tag in tags}
