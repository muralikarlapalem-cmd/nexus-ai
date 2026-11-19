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

    if payload.get("type") == "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="A refresh token can't be used to call the API",
                            headers={"WWW-Authenticate": "Bearer"})

    user_id_str: str | None = payload.get("sub")
    if not user_id_str:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    if payload.get("_sso"):
        user = await _sso_user(payload, db)
    else:
        user = None
        try:
            result = await db.execute(select(User).where(User.id == uuid.UUID(user_id_str)))
            user = result.scalar_one_or_none()
        except ValueError:
            pass
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated",
        )

    return user


async def _sso_user(payload: dict, db: AsyncSession) -> User:
    """
    The NEXUS user for a Keycloak sign-in, created on first sign-in.
    Keycloak decides the role (realm roles nexus-admin / nexus-agent /
    nexus-analyst), so it's brought up to date on every request.
    An existing local account with the same e-mail is linked only when
    Keycloak has verified that e-mail.
    """
    from app.core.sso import role_from_token
    from app.models.audit_log import AuditLog

    kc_id = payload["sub"]
    email = (payload.get("email") or "").lower()
    role = role_from_token(payload)

    user = (await db.execute(select(User).where(User.keycloak_id == kc_id))).scalar_one_or_none()
    if user is None and email and payload.get("email_verified"):
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is not None:
            user.keycloak_id = kc_id
            db.add(AuditLog(user_id=user.id, action="user.sso_linked", resource_type="user",
                            resource_id=str(user.id), details={"keycloak_id": kc_id}))
            logger.info(f"Linked local account {user.username} to Keycloak")

    if user is None:
        if not email:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Your Keycloak account has no e-mail address")
        if (await db.execute(select(User).where(User.email == email))).scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="A NEXUS account already uses this e-mail. Verify the e-mail in "
                                       "Keycloak to link the two, or ask an admin.")
        username = payload.get("preferred_username") or email.split("@")[0]
        taken = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if taken:
            username = f"{username}-sso"
        user = User(email=email, username=username, full_name=payload.get("name"),
                    keycloak_id=kc_id, is_keycloak_user=True, role=role)
        db.add(user)
        await db.flush()
        db.add(AuditLog(user_id=user.id, action="user.sso_created", resource_type="user",
                        resource_id=str(user.id), details={"role": role.value}))
        logger.info(f"First SSO sign-in: created {username} ({role.value})")
    elif user.role != role:
        db.add(AuditLog(user_id=user.id, action="user.role_change", resource_type="user",
                        resource_id=str(user.id),
                        details={"old_role": user.role.value, "new_role": role.value, "via": "keycloak"}))
        user.role = role
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
