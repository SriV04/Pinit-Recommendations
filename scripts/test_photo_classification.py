#!/usr/bin/env python3
"""Test photo classification functionality in isolation."""
import logging
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pinit.api.services.proximal_service import classify_location_photo
from pinit.config.secrets import GOOGLE_MAPS_API_KEY

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def test_photo_classification(place_id: str):
    """Test photo classification for a specific place ID."""
    logger.info("=" * 80)
    logger.info("Testing photo classification for place ID: %s", place_id)
    logger.info("=" * 80)
    
    # Check API keys
    if not GOOGLE_MAPS_API_KEY:
        logger.error("GOOGLE_MAPS_API_KEY not found in environment")
        return
    
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        logger.warning("OPENAI_API_KEY not found - classification will be skipped")
    else:
        logger.info("OPENAI_API_KEY found ✓")
    
    logger.info("GOOGLE_MAPS_API_KEY found ✓")
    
    # Run classification
    photo_ref, photo_score, photo_bytes, content_type = classify_location_photo(
        place_id, GOOGLE_MAPS_API_KEY
    )
    
    # Print results
    logger.info("=" * 80)
    logger.info("RESULTS:")
    logger.info("-" * 80)
    logger.info("Photo Reference: %s", photo_ref)
    logger.info("Photo Score: %s", photo_score)
    logger.info("Photo Downloaded: %s", "Yes" if photo_bytes else "No")
    if photo_bytes:
        logger.info("Photo Size: %d bytes", len(photo_bytes))
        logger.info("Content Type: %s", content_type)
    logger.info("=" * 80)
    
    # Status summary
    if not photo_ref:
        logger.error("❌ FAILED: No photo found for this location")
        return False
    
    if not photo_bytes:
        logger.error("❌ FAILED: Photo reference found but download failed")
        return False
    
    if photo_score is None:
        logger.warning("⚠️  WARNING: Photo downloaded but classification returned None")
        logger.warning("    Check OPENAI_API_KEY and OpenAI response parsing")
        return False
    
    logger.info("✅ SUCCESS: Photo classified with score %d", photo_score)
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        # Default test place ID (Brighton Japanese restaurant)
        place_id = "ChIJKUQkc4uFdUgRPSpCjkGi100"
        logger.info("No place ID provided, using default: %s", place_id)
    else:
        place_id = sys.argv[1]
    
    success = test_photo_classification(place_id)
    sys.exit(0 if success else 1)
