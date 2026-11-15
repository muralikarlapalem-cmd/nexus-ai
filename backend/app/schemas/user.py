"""
NEXUS AI · app/schemas/user.py
Pydantic v2 schemas for User auth and profile.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.models.user import UserRole


class UserCreate(BaseModel):
    """
    Local user registration (dev / fallback only).
    New users always get the `user` role; there is deliberately no `role` field,
    and unknown fields are rejected so a request asking for one gets a clear 422.
    Promote via PATCH /users/{id}/role (admin) or `python -m app.cli make-admin`.
    """
    model_config = {"extra": "forbid"}

    email: EmailStr
    username: str = Field(..., min_length=3, max_length=50)
    full_name: str | None = Field(None, max_length=255)
    password: str = Field(..., min_length=8)

class UserRoleUpdate(BaseModel):
    """Admin-only: change a user's role."""
    role: UserRole


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    username: str
    full_name: str | None
    role: UserRole
    is_active: bool
    is_keycloak_user: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    """Returned on successful login."""
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int  # seconds


class LoginRequest(BaseModel):
    """Local login (dev only — prod uses Keycloak redirect)."""
    username: str
    password: str
