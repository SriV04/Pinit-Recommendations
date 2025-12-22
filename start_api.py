#!/usr/bin/env python3
"""
Startup script for the Proximal Recommendations API.
"""

import sys
import uvicorn
from pathlib import Path

# Add src to path
src_path = Path(__file__).parent / "src"
sys.path.insert(0, str(src_path))

if __name__ == "__main__":
    print("Starting Pinit Proximal Recommendations API...")
    print("API Documentation: http://localhost:8000/docs")
    print("Health Check: http://localhost:8000/health")
    print("\nPress CTRL+C to stop\n")
    
    uvicorn.run(
        "api.proximal_api:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
