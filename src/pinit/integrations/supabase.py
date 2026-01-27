from typing import Optional, List, Dict, Any
from urllib.parse import urlparse
import logging
from supabase import create_client, Client
from pinit.config.secrets import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

_CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _extension_for_content_type(content_type: Optional[str]) -> str:
    if not content_type:
        return ""
    normalized = content_type.split(";", 1)[0].strip().lower()
    return _CONTENT_TYPE_EXTENSIONS.get(normalized, "")


class SupabaseService:
    """Basic Supabase service for CRUD operations"""
    
    def __init__(self):
        logger = logging.getLogger(__name__)
        url = (SUPABASE_URL or "").strip()
        # Use service role key for full access
        key = (SUPABASE_SERVICE_ROLE_KEY or "").strip()
        
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in environment or Secret Manager")
        
        # Ensure URL has trailing slash for storage endpoints
        if not url.endswith("/"):
            url = url + "/"
        
        # Basic validation + helpful debug logging without leaking secrets
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            masked = url[:32] + ("…" if len(url) > 32 else "")
            logger.error(f"Invalid SUPABASE_URL format: '{masked}'. Expected like 'https://<ref>.supabase.co'")
            raise ValueError("Invalid SUPABASE_URL format")
        logger.debug(f"Initializing SupabaseService for host: {parsed.netloc}")
        
        self.client: Client = create_client(url, key)
    
    # ==================== TAGS CRUD ====================
    
    def create_tag(self, text: str, prompt_description: Optional[str] = None, 
                   tag_type: Optional[str] = None, colour: Optional[str] = None) -> Dict[str, Any]:
        """Create a new tag"""
        data = {"text": text}
        if prompt_description:
            data["prompt_description"] = prompt_description
        if tag_type:
            data["tag_type"] = tag_type
        if colour:
            data["Colour"] = colour
            
        response = self.client.table("tags").insert(data).execute()
        return response.data[0] if response.data else None
    
    def get_tag(self, tag_id: str) -> Optional[Dict[str, Any]]:
        """Get a tag by ID"""
        response = self.client.table("tags").select("*").eq("tag_id", tag_id).execute()
        return response.data[0] if response.data else None
    
    def get_all_tags(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get all tags"""
        response = self.client.table("tags").select("*").limit(limit).execute()
        return response.data
    
    def update_tag(self, tag_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Update a tag"""
        response = self.client.table("tags").update(kwargs).eq("tag_id", tag_id).execute()
        return response.data[0] if response.data else None
    
    def delete_tag(self, tag_id: str) -> bool:
        """Delete a tag"""
        response = self.client.table("tags").delete().eq("tag_id", tag_id).execute()
        return len(response.data) > 0
    
    # ==================== LOCATIONS CRUD ====================
    
    def create_location(self, name: str, **kwargs) -> Dict[str, Any]:
        """Create a new location"""
        data = {"name": name, **kwargs}
        response = self.client.table("locations").insert(data).execute()
        return response.data[0] if response.data else None
    
    def get_location(self, location_id: int) -> Optional[Dict[str, Any]]:
        """Get a location by ID"""
        response = self.client.table("locations").select("*").eq("location_id", location_id).execute()
        return response.data[0] if response.data else None
    
    def get_location_by_google_place_id(self, google_place_id: str) -> Optional[Dict[str, Any]]:
        """Get a location by Google Place ID"""
        response = self.client.table("locations").select("*").eq("google_place_id", google_place_id).execute()
        return response.data[0] if response.data else None
    
    def get_locations(self, limit: int = 100, offset: int = 0, **filters) -> List[Dict[str, Any]]:
        """Get locations with optional filters"""
        query = self.client.table("locations").select("*").limit(limit).offset(offset)
        for key, value in filters.items():
            query = query.eq(key, value)
        response = query.execute()
        return response.data
    
    def get_location_without_emoji(self) -> List[Dict[str, Any]]:
        """Get locations without an emoji assigned"""
        query = self.client.table("locations").select("*").is_("emoji", None)
        response = query.execute()
        return response.data
    
    def update_location(self, location_id: int, **kwargs) -> Optional[Dict[str, Any]]:
        """Update a location"""
        response = self.client.table("locations").update(kwargs).eq("location_id", location_id).execute()
        return response.data[0] if response.data else None
    
    def delete_location(self, location_id: int) -> bool:
        """Delete a location"""
        response = self.client.table("locations").delete().eq("location_id", location_id).execute()
        return len(response.data) > 0

    def upload_location_photo(
        self,
        location_id: int,
        image_bytes: bytes,
        content_type: Optional[str] = None,
    ) -> Any:
        """Upload a location photo to the location_photos bucket."""
        import logging
        logger = logging.getLogger(__name__)

        file_options = {
            "content-type": content_type or "image/jpeg",
            "upsert": "true",
        }
        extension = _extension_for_content_type(content_type)
        object_name = f"{location_id}{extension}"

        logger.info(
            "Uploading to Supabase storage: bucket='location_photos', object='%s', size=%d bytes",
            object_name,
            len(image_bytes),
        )
        logger.debug("File options: %s", file_options)

        try:
            result = self.client.storage.from_("location_photos").upload(
                object_name,
                image_bytes,
                file_options,
            )
            logger.info("Supabase storage upload response: %s", result)

            # Update the location record saying image is stored
            try:
                self.update_location(location_id, image_stored=True)
                logger.info("Successfully updated image_url for location %s", location_id)
            except Exception as update_exc:
                logger.error(
                    "Failed to update image_url for location %s: %s",
                    location_id,
                    update_exc,
                    exc_info=True,
                )
                # Don't raise - photo upload succeeded, URL update is secondary

            return result
        except Exception as exc:
            logger.error(
                "Supabase storage upload failed for object '%s': %s",
                object_name,
                exc,
                exc_info=True,
            )
            raise
    
    # ==================== LOCATION_TAGS CRUD ====================
    
    def create_location_tag(self, location_id: int, tag_id: str, score: Optional[float] = None,
                           source: Optional[str] = None, metadata: Optional[Dict] = None) -> Dict[str, Any]:
        """Create a location-tag association"""
        data = {
            "location_id": location_id,
            "tag_id": tag_id
        }
        if score is not None:
            data["score"] = score
        if source:
            data["source"] = source
        if metadata:
            data["metadata"] = metadata
            
        response = self.client.table("location_tags").insert(data).execute()
        return response.data[0] if response.data else None
    
    def get_location_tag(self, location_tag_id: int) -> Optional[Dict[str, Any]]:
        """Get a location_tag by ID"""
        response = self.client.table("location_tags").select("*").eq("id", location_tag_id).execute()
        return response.data[0] if response.data else None
    
    def get_location_tags(self, location_id: Optional[int] = None, 
                         tag_id: Optional[str] = None,
                         limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get location tags, optionally filtered, with optional limit."""
        query = self.client.table("location_tags").select("*")
        if location_id:
            query = query.eq("location_id", location_id)
        if tag_id:
            query = query.eq("tag_id", tag_id)
        if limit:
            query = query.limit(limit)
        response = query.execute()
        return response.data
    
    def update_location_tag(self, location_tag_id: int, **kwargs) -> Optional[Dict[str, Any]]:
        """Update a location_tag"""
        response = self.client.table("location_tags").update(kwargs).eq("id", location_tag_id).execute()
        return response.data[0] if response.data else None
    
    def delete_location_tag(self, location_tag_id: int) -> bool:
        """Delete a location_tag"""
        response = self.client.table("location_tags").delete().eq("id", location_tag_id).execute()
        return len(response.data) > 0
    
    # ==================== RECOMMENDATION_CANDIDATES CRUD ====================
    
    def create_recommendation_candidate(self, run_id: str, location_id: int, score: float,
                                       rank: Optional[int] = None, reason: Optional[Dict] = None,
                                       features: Optional[Dict] = None) -> Dict[str, Any]:
        """Create a recommendation candidate"""
        data = {
            "run_id": run_id,
            "location_id": location_id,
            "score": score
        }
        if rank is not None:
            data["rank"] = rank
        if reason:
            data["reason"] = reason
        if features:
            data["features"] = features
            
        response = self.client.table("recommendation_candidates").insert(data).execute()
        return response.data[0] if response.data else None
    
    def get_recommendation_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        """Get a recommendation candidate by ID"""
        response = self.client.table("recommendation_candidates").select("*").eq("candidate_id", candidate_id).execute()
        return response.data[0] if response.data else None
    
    def get_recommendation_candidates(self, run_id: Optional[str] = None,
                                     location_id: Optional[int] = None,
                                     limit: int = 100) -> List[Dict[str, Any]]:
        """Get recommendation candidates, optionally filtered"""
        query = self.client.table("recommendation_candidates").select("*").limit(limit)
        if run_id:
            query = query.eq("run_id", run_id)
        if location_id:
            query = query.eq("location_id", location_id)
        response = query.execute()
        return response.data
    
    def update_recommendation_candidate(self, candidate_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Update a recommendation candidate"""
        response = self.client.table("recommendation_candidates").update(kwargs).eq("candidate_id", candidate_id).execute()
        return response.data[0] if response.data else None
    
    def delete_recommendation_candidate(self, candidate_id: str) -> bool:
        """Delete a recommendation candidate"""
        response = self.client.table("recommendation_candidates").delete().eq("candidate_id", candidate_id).execute()
        return len(response.data) > 0
    
    # ==================== USER_TAG_AFFINITIES CRUD ====================
    
    def create_user_tag_affinity(self, user_id: str, tag_id: str, affinity: float,
                                evidence: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Create or upsert a user tag affinity.
        
        Args:
            user_id: User UUID
            tag_id: Tag UUID
            affinity: User preference score (0-100 scale)
            evidence: Optional metadata about how affinity was calculated
        """
        data = {
            "user_id": user_id,
            "tag_id": tag_id,
            "affinity": affinity
        }
        if evidence:
            data["evidence"] = evidence
            
        response = self.client.table("user_tag_affinities").upsert(data).execute()
        return response.data[0] if response.data else None
    
    def get_user_tag_affinity(self, user_id: str, tag_id: str) -> Optional[Dict[str, Any]]:
        """Get a user tag affinity"""
        response = (self.client.table("user_tag_affinities")
                   .select("*")
                   .eq("user_id", user_id)
                   .eq("tag_id", tag_id)
                   .execute())
        return response.data[0] if response.data else None
    
    def get_user_tag_affinities(self, user_id: Optional[str] = None,
                               tag_id: Optional[str] = None,
                               min_affinity: Optional[float] = None) -> List[Dict[str, Any]]:
        """
        Get user tag affinities with optional filters.
        
        Args:
            user_id: Optional user UUID to filter by
            tag_id: Optional tag UUID to filter by
            min_affinity: Optional minimum affinity threshold (0-100 scale)
        
        Returns:
            List of affinity records with affinity values on 0-100 scale
        """
        query = self.client.table("user_tag_affinities").select("*")
        if user_id:
            query = query.eq("user_id", user_id)
        if tag_id:
            query = query.eq("tag_id", tag_id)
        if min_affinity is not None:
            query = query.gte("affinity", min_affinity)
        response = query.execute()
        return response.data
    
    def update_user_tag_affinity(self, user_id: str, tag_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Update a user tag affinity"""
        response = (self.client.table("user_tag_affinities")
                   .update(kwargs)
                   .eq("user_id", user_id)
                   .eq("tag_id", tag_id)
                   .execute())
        return response.data[0] if response.data else None
    
    def delete_user_tag_affinity(self, user_id: str, tag_id: str) -> bool:
        """Delete a user tag affinity"""
        response = (self.client.table("user_tag_affinities")
                   .delete()
                   .eq("user_id", user_id)
                   .eq("tag_id", tag_id)
                   .execute())
        return len(response.data) > 0


# Singleton instance
_supabase_service = None


def get_supabase_service() -> SupabaseService:
    """Get or create the singleton SupabaseService instance"""
    global _supabase_service
    if _supabase_service is None:
        _supabase_service = SupabaseService()
    return _supabase_service
