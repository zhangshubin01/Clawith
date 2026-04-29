"""Login related schemas."""

import uuid
from pydantic import BaseModel, Field

from app.schemas.auth.common import UserOut, IdentityOut


class UserLogin(BaseModel):
    """Login request schema."""

    login_identifier: str = Field(description="Email address, phone or username for login")
    password: str
    tenant_id: uuid.UUID | None = Field(
        None, description="Optional: when set, restrict login to users of this tenant"
    )


class TokenResponse(BaseModel):
    """Successful login response."""

    access_token: str
    token_type: str = "bearer"
    user: UserOut
    identity: IdentityOut | None = None
    needs_company_setup: bool = False
    tenant_name: str | None = None


class TenantChoice(BaseModel):
    """Multi-tenant login: tenant selection info."""

    tenant_id: uuid.UUID | None
    tenant_name: str
    tenant_slug: str


class MultiTenantResponse(BaseModel):
    """Response when multiple tenants match the same login identifier."""

    requires_tenant_selection: bool = True
    login_identifier: str
    tenants: list[TenantChoice]
