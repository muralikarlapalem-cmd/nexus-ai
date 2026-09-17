"""
NEXUS AI · app/core/deps.py
FastAPI dependency functions — inject current user, enforce RBAC, provide DB session.

Usage in route:
    @router.get("/admin-only")
    async def admin_route(user: User = Depends(require_role(UserRole.ADMIN))):
        ...
"""

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.database import get_async_db
from app.models.user import User, UserRole

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_async_db),
) -> User:
    """
    Extract and validate the Bearer token, return the matching User record.
    Raises 401 if token is missing/invalid, 403 if user is inactive.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication credentials not provided",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = await decode_token(credentials.credentials)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Extract user identifier from token claims
    # Keycloak puts the UUID in `sub`; local tokens do the same.
    user_id_str: str | None = payload.get("sub")
    keycloak_id: str | None = payload.get("sub") if payload.get("iss") else None

    if not user_id_str:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    # Try UUID lookup first, then Keycloak ID
    user: User | None = None
    try:
        uid = uuid.UUID(user_id_str)
        result = await db.execute(select(User).where(User.id == uid))
        user = result.scalar_one_or_none()
    except ValueError:
        pass

    if user is None and keycloak_id:
        result = await db.execute(select(User).where(User.keycloak_id == keycloak_id))
        user = result.scalar_one_or_none()

    if user is None:
        # Auto-provision Keycloak users on first access
        email = payload.get("email", "")
        username = payload.get("preferred_username", email.split("@")[0])
        if email:
            user = User(
                email=email,
                username=username,
                full_name=payload.get("name"),
                keycloak_id=user_id_str,
                is_keycloak_user=True,
            )
            db.add(user)
            await db.flush()
            logger.info(f"Auto-provisioned Keycloak user: {email}")
        else:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated",
        )

    return user


def require_role(*roles: UserRole):
    """
    Returns a dependency that enforces one of the specified roles.

    Example:
        @router.delete("/{id}", dependencies=[Depends(require_role(UserRole.ADMIN))])
    """
    async def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of: {[r.value for r in roles]}",
            )
        return current_user
    return role_checker


# Convenience aliases
require_admin = require_role(UserRole.ADMIN)
require_agent_or_admin = require_role(UserRole.AGENT, UserRole.ADMIN)
require_analyst_or_above = require_role(UserRole.ANALYST, UserRole.AGENT, UserRole.ADMIN)
