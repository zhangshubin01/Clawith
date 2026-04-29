"""Password related schemas."""

from pydantic import BaseModel, EmailStr, Field


class ForgotPasswordRequest(BaseModel):
    """Forgot password request."""

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """Reset password request with token."""

    token: str = Field(min_length=20, max_length=512)
    new_password: str = Field(min_length=6, max_length=128)


class VerifyEmailRequest(BaseModel):
    """Verify email request with token."""

    token: str = Field(min_length=6, max_length=512)


class ResendVerificationRequest(BaseModel):
    """Resend verification email request."""

    email: EmailStr
