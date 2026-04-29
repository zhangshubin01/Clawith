"""Registration related schemas."""

import uuid
from pydantic import BaseModel, EmailStr, Field

from app.schemas.auth.common import UserOut


class UserRegister(BaseModel):
    """Legacy combined registration - kept for backward compatibility."""

    username: str = Field(min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    display_name: str | None = None
    invitation_code: str | None = None
    # SSO registration fields
    provider: str | None = Field(
        None, description="Provider type for SSO registration (feishu, dingtalk, etc.)"
    )
    provider_code: str | None = Field(
        None, description="OAuth code for SSO registration"
    )


class RegisterInitRequest(BaseModel):
    """Step 1: Initialize registration with account credentials."""

    username: str = Field(min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    display_name: str | None = None
    target_tenant_id: uuid.UUID | None = None


class RegisterInitResponse(BaseModel):
    """Response after step 1 - user created, needs email verification."""

    user_id: uuid.UUID
    email: str
    access_token: str
    message: str = "Registration initiated. Please verify your email."
    user: UserOut  # Include full user info
    needs_company_setup: bool = True
    target_tenant_id: uuid.UUID | None = None


class RegisterCompleteRequest(BaseModel):
    """Step 3: Complete registration after email verification."""

    token: str = Field(
        min_length=6, max_length=512, description="Email verification code"
    )


class RegisterCompleteResponse(BaseModel):
    """Response after successful registration completion."""

    access_token: str
    token_type: str = "bearer"
    user: UserOut
    needs_company_setup: bool = False


class SSORegisterRequest(BaseModel):
    """SSO registration - completely separate from normal registration."""

    provider: str = Field(description="Provider type (feishu, dingtalk, etc.)")
    code: str = Field(description="OAuth authorization code from provider")
    invitation_code: str | None = None


class NeedsVerificationResponse(BaseModel):
    """Response when user needs to verify email before continuing."""

    needs_verification: bool = True
    email: str
    message: str = (
        "Email already registered but not verified. Please enter the verification code."
    )
