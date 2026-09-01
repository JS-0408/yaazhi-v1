"""
Yaazhi API Package
==================
FastAPI application, routes, and middleware.

Usage:
    from api.main import app          # FastAPI application instance
    from api.middleware import setup_middleware
"""

# Re-export the FastAPI app so external tools (uvicorn, gunicorn) can
# locate it as  api:app  without needing api.main.
from api.main import app

__all__ = ["app"]
