"""
NEXUS AI · app/api/v1/auth.py
Authentication endpoints:
  POST /auth/login        — local HS256 login (dev/testing)
  POST /auth/refresh      — refresh access token
  GET  /auth/me           — return current user profile
  POST /auth/register     — local user registration (dev only)
  GET  /auth/sso-config   — whether Keycloak sign-in is ready, and where it is
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.deps import get_current_user
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_local_token,
    hash_password,
    verify_password,
)
from app.database import get_async_db
from app.models.audit_log import AuditLog
from app.models.user import User, UserRole
from app.schemas.user import LoginRequest, TokenResponse, UserCreate, UserResponse

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/login", response_model=TokenResponse)
async def local_login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Local password login — for dev/testing only.
    Production traffic authenticates via Keycloak SSO.
    """
    if settings.is_production:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Local login disabled in production. Use Keycloak SSO.",
        )

    result = await db.execute(select(User).where(User.username == body.username))
    user: User | None = result.scalar_one_or_none()

    if not user or not user.hashed_password or not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account deactivated")

    # Update last login
    user.last_login_at = datetime.now(timezone.utc)

    # Audit log
    db.add(AuditLog(
        user_id=user.id,
        action="auth.login",
        resource_type="user",
        resource_id=str(user.id),
        details={"method": "local"},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    ))

    access_token = create_access_token(
        subject=str(user.id),
        extra_claims={"role": user.role.value, "email": user.email},
    )
    refresh_token = create_refresh_token(subject=str(user.id))

    logger.info(f"Local login: {user.username}")
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.jwt_access_token_expire_minutes * 60,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(refresh_token: str):
    """Exchange a refresh token for a new access token."""
    try:
        payload = decode_local_token(refresh_token)
        if payload.get("type") != "refresh":
            raise ValueError("Not a refresh token")
        user_id = payload["sub"]
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))

    access_token = create_access_token(subject=user_id)
    return TokenResponse(
        access_token=access_token,
        expires_in=settings.jwt_access_token_expire_minutes * 60,
    )


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """Return the currently authenticated user's profile."""
    return current_user


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register_local_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_async_db),
):
    """Register a local user — dev/testing only."""
    if settings.is_production:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Self-registration disabled in production.",
        )

    # Check uniqueness
    existing = await db.execute(
        select(User).where((User.email == body.email) | (User.username == body.username))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email or username already registered",
        )

    user = User(
        email=body.email,
        username=body.username,
        full_name=body.full_name,
        role=UserRole.USER,   # never caller-controlled; promote via admin endpoint or CLI
        hashed_password=hash_password(body.password),
        is_keycloak_user=False,
    )
    db.add(user)
    await db.flush()
    logger.info(f"Registered local user: {user.username}")
    return user


@router.get("/sso-config")
async def sso_config():
    """
    What the login page needs for "Sign in with Keycloak": whether SSO is ready,
    Keycloak's address (as the browser sees it), the realm and the web app's
    client ID. Public: it holds nothing secret.
    """
    from app.core.sso import sso_status
    return await sso_status()
