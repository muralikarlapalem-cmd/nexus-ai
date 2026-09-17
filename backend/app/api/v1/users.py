"""
NEXUS AI · app/api/v1/users.py
User management — admin role assignment and user listing.
"""

import math
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_admin
from app.database import get_async_db
from app.models.audit_log import AuditLog
from app.models.user import User, UserRole
from app.schemas.user import UserResponse, UserRoleUpdate

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("/", response_model=list[UserResponse])
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_admin),
):
    """Admin-only: list all users."""
    offset = (page - 1) * page_size
    result = await db.execute(
        select(User).order_by(User.created_at.desc()).offset(offset).limit(page_size)
    )
    return result.scalars().all()


@router.get("/me", response_model=UserResponse)
async def get_my_profile(
    current_user: User = Depends(get_current_user),
):
    """Return the currently authenticated user's own profile."""
    return current_user


@router.patch("/{user_id}/role", response_model=UserResponse)
async def update_user_role(
    user_id: uuid.UUID,
    body: UserRoleUpdate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_admin),
):
    """Admin-only: change a user's role."""
    result = await db.execute(select(User).where(User.id == user_id))
    user: User | None = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if user.id == current_user.id and body.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot demote your own account",
        )

    old_role = user.role
    user.role = body.role

    db.add(AuditLog(
        user_id=current_user.id,
        action="user.role_change",
        resource_type="user",
        resource_id=str(user.id),
        details={"old_role": old_role.value, "new_role": body.role.value},
    ))

    logger.info(f"User {user.username} role: {old_role} → {body.role} by {current_user.username}")
    return user