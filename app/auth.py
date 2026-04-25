"""
API key authentication for admin endpoints.

Usage:
    from app.auth import require_admin

    @router.get("/protected", dependencies=[Depends(require_admin)])
    async def protected_endpoint(): ...

Set the ADMIN_API_KEY environment variable. If unset in production
(ENVIRONMENT != "development"), all admin endpoints will reject requests.
"""

import hmac
import os

from fastapi import Header, HTTPException

ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "production")


async def require_admin(x_admin_key: str = Header("")) -> None:
    """Dependency that enforces API key authentication on admin endpoints."""
    if not ADMIN_API_KEY:
        if ENVIRONMENT == "development":
            return  # Allow unauthenticated access in dev mode
        raise HTTPException(
            status_code=503,
            detail="ADMIN_API_KEY not configured. Set it in .env to enable admin access.",
        )
    if not hmac.compare_digest(x_admin_key, ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Key header")
