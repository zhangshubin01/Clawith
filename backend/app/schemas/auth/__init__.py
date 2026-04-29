"""Authentication schemas."""

from app.schemas.auth.login import (
    UserLogin,
    TokenResponse,
    TenantChoice,
    MultiTenantResponse,
)
from app.schemas.auth.register import (
    UserRegister,
    RegisterInitRequest,
    RegisterInitResponse,
    RegisterCompleteRequest,
    RegisterCompleteResponse,
    SSORegisterRequest,
    NeedsVerificationResponse,
)
from app.schemas.auth.password import (
    ForgotPasswordRequest,
    ResetPasswordRequest,
    VerifyEmailRequest,
    ResendVerificationRequest,
)
from app.schemas.auth.sso import (
    OAuthAuthorizeResponse,
    OAuthCallbackRequest,
    IdentityBindRequest,
    IdentityUnbindRequest,
    IdentityProviderOut,
)
from app.schemas.auth.tenant import (
    TenantSwitchRequest,
    TenantSwitchResponse,
)
from app.schemas.auth.common import (
    IdentityOut,
    UserOut,
    UserUpdate,
)

__all__ = [
    # Login
    "UserLogin",
    "TokenResponse",
    "TenantChoice",
    "MultiTenantResponse",
    # Register
    "UserRegister",
    "RegisterInitRequest",
    "RegisterInitResponse",
    "RegisterCompleteRequest",
    "RegisterCompleteResponse",
    "SSORegisterRequest",
    "NeedsVerificationResponse",
    # Password
    "ForgotPasswordRequest",
    "ResetPasswordRequest",
    "VerifyEmailRequest",
    "ResendVerificationRequest",
    # SSO
    "OAuthAuthorizeResponse",
    "OAuthCallbackRequest",
    "IdentityBindRequest",
    "IdentityUnbindRequest",
    "IdentityProviderOut",
    # Tenant
    "TenantSwitchRequest",
    "TenantSwitchResponse",
    # Common
    "IdentityOut",
    "UserOut",
    "UserUpdate",
]
