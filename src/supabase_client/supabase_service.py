import os
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
from supabase import create_client, Client

# Load environment variables
load_dotenv()


class SupabaseService:
    """Basic Supabase service for CRUD operations"""
    
    def __init__(self):
        url = os.getenv("SUPABASE_URL")
        # Use service role key for full access
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in environment")
        
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
    
    def get_locations(self, limit: int = 100, offset: int = 0, **filters) -> List[Dict[str, Any]]:
        """Get locations with optional filters"""
        query = self.client.table("locations").select("*").limit(limit).offset(offset)
        for key, value in filters.items():
            query = query.eq(key, value)
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
                         tag_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get location tags, optionally filtered by location_id or tag_id"""
        query = self.client.table("location_tags").select("*")
        if location_id:
            query = query.eq("location_id", location_id)
        if tag_id:
            query = query.eq("tag_id", tag_id)
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
        """Create or upsert a user tag affinity"""
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
        """Get user tag affinities with optional filters"""
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
