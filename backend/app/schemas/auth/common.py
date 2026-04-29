"""Common authentication schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class IdentityOut(BaseModel):
    """Global identity information."""

    id: uuid.UUID
    email: str | None = None
    phone: str | None = None
    username: str | None = None
    is_active: bool
    is_platform_admin: bool
    email_verified: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UserOut(BaseModel):
    """User profile response."""

    id: uuid.UUID
    identity_id: uuid.UUID | None = None
    username: str | None = None
    email: str | None = None
    display_name: str
    avatar_url: str | None = None
    role: str
    tenant_id: uuid.UUID | None = None
    title: str | None = None
    primary_mobile: str | None = None
    registration_source: str | None = None
    is_active: bool
    email_verified: bool = True
    created_at: datetime

    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    """User profile update request."""

    username: str | None = None
    email: EmailStr | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    title: str | None = None
    primary_mobile: str | None = None
