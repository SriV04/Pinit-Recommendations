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
    import os
    
    # Cloud Run sets PORT=8080, local dev can use 8000
    port = int(os.getenv("PORT", "8080"))
    
    print("Starting Pinit Proximal Recommendations API...")
    print(f"API Documentation: http://localhost:{port}/docs")
    print(f"Health Check: http://localhost:{port}/health")
    print("\nPress CTRL+C to stop\n")
    
    uvicorn.run(
        "api.proximal_api:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_level="info"
    )
