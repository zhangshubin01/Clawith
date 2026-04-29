"""SSO/OAuth related schemas."""

import uuid
from datetime import datetime
from pydantic import BaseModel, Field


class OAuthAuthorizeResponse(BaseModel):
    """OAuth authorization URL response."""

    authorization_url: str


class OAuthCallbackRequest(BaseModel):
    """OAuth callback request."""

    code: str
    state: str


class IdentityBindRequest(BaseModel):
    """Bind external identity request."""

    provider_type: str
    code: str  # OAuth code for binding


class IdentityUnbindRequest(BaseModel):
    """Unbind external identity request."""

    provider_type: str


class IdentityProviderOut(BaseModel):
    """Identity provider configuration response."""

    id: uuid.UUID
    provider_type: str
    name: str
    is_active: bool
    sso_login_enabled: bool = False
    config: dict | None = None
    tenant_id: uuid.UUID | None = None
    updated_at: datetime | None = None
    created_at: datetime
    sso_domain: str | None = None

    model_config = {"from_attributes": True}
