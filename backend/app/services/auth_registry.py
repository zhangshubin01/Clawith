"""Authentication provider registry and factory.

This module provides a centralized way to manage and instantiate auth providers.
"""

from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import decrypt_data
from app.models.identity import IdentityProvider
from app.services.auth_provider import (
    PROVIDER_CLASSES,
    BaseAuthProvider,
)
from app.services.identity_provider_lookup import get_preferred_identity_provider

settings = get_settings()

# 需要解密的 config 敏感字段（与 enterprise.py 中 _SENSITIVE_CONFIG_KEYS 保持一致）
_SENSITIVE_CONFIG_KEYS = {"client_secret", "app_secret"}


def _decrypt_config_secrets(config: dict) -> dict:
    """对 provider config 中被加密的敏感字段进行解密，供 auth provider 使用。

    encrypt_data 加密的密文以 Base64 编码且带 \\x01\\xca\\xfe 魔数头，
    解码后以 "AW" 开头。未加密的旧数据或解密失败时直接返回原值。
    """
    if not config:
        return config
    for field in _SENSITIVE_CONFIG_KEYS:
        value = config.get(field)
        if value and isinstance(value, str):
            try:
                config[field] = decrypt_data(value, settings.SECRET_KEY)
            except Exception:
                # 可能是未加密的旧数据或解密失败，保持原值
                pass
    return config


class AuthProviderRegistry:
    """Registry for managing authentication provider instances.

    This class provides a factory method to create provider instances
    and caches them for reuse.
    """

    def __init__(self):
        self._cache: dict[str, BaseAuthProvider] = {}

    async def get_provider(
        self, db: AsyncSession, provider_type: str, tenant_id: str | None = None
    ) -> BaseAuthProvider | None:
        """Get or create an authentication provider instance.

        Args:
            db: Database session
            provider_type: The type of provider (feishu, dingtalk, etc.)
            tenant_id: Optional tenant ID for tenant-specific providers

        Returns:
            Provider instance or None if provider type is not supported
        """
        # Check cache first
        cache_key = f"{provider_type}:{tenant_id or 'global'}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        # Try to get provider config from database
        provider_model = await get_preferred_identity_provider(
            db,
            provider_type,
            tenant_id,
            is_active=True,
        )

        # Create provider instance
        provider = self._create_provider(provider_type, provider_model)
        if provider:
            self._cache[cache_key] = provider

        return provider

    def _create_provider(
        self, provider_type: str, provider_model: IdentityProvider | None
    ) -> BaseAuthProvider | None:
        """Create a provider instance based on type.

        Args:
            provider_type: The type of provider
            provider_model: Optional IdentityProvider model from database

        Returns:
            Provider instance or None
        """
        provider_class = PROVIDER_CLASSES.get(provider_type)
        if not provider_class:
            return None

        config = provider_model.config if provider_model else {}
        # 解密 DB 中加密存储的 OAuth2 client_secret/app_secret，恢复明文供 auth provider 使用（#82 修复）
        config = _decrypt_config_secrets(config)
        return provider_class(provider=provider_model, config=config)

    async def list_providers(
        self, db: AsyncSession, tenant_id: str | None = None
    ) -> list[IdentityProvider]:
        """List all available identity providers.

        Args:
            db: Database session
            tenant_id: Optional tenant ID to filter by

        Returns:
            List of IdentityProvider records
        """
        query = select(IdentityProvider).where(IdentityProvider.is_active == True)

        if tenant_id:
            # Only include tenant-specific ones
            query = query.where(IdentityProvider.tenant_id == tenant_id)
        else:
            # Public OAuth login should only expose global providers.
            query = query.where(IdentityProvider.tenant_id.is_(None))

        result = await db.execute(query)
        return list(result.scalars().all())

    async def create_provider(
        self,
        db: AsyncSession,
        provider_type: str,
        name: str,
        config: dict[str, Any],
        tenant_id: str | None = None,
    ) -> IdentityProvider:
        """Create a new identity provider.

        Args:
            db: Database session
            provider_type: Type of provider
            name: Display name
            config: Provider configuration
            tenant_id: Optional tenant ID for tenant-specific provider

        Returns:
            Created IdentityProvider record
        """
        provider = IdentityProvider(
            provider_type=provider_type,
            name=name,
            is_active=True,
            config=config,
            tenant_id=tenant_id,
        )
        db.add(provider)
        await db.flush()

        # Clear cache for this provider type
        self._clear_cache(provider_type)

        return provider

    async def update_provider(
        self,
        db: AsyncSession,
        provider_id: str,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        is_active: bool | None = None,
    ) -> IdentityProvider | None:
        """Update an existing identity provider.

        Args:
            db: Database session
            provider_id: Provider ID
            name: New display name
            config: New configuration
            is_active: New active status

        Returns:
            Updated IdentityProvider or None if not found
        """
        result = await db.execute(
            select(IdentityProvider).where(IdentityProvider.id == provider_id)
        )
        provider = result.scalar_one_or_none()

        if not provider:
            return None

        if name is not None:
            provider.name = name
        if config is not None:
            provider.config = config
        if is_active is not None:
            provider.is_active = is_active

        await db.flush()

        # Clear cache
        self._clear_cache(provider.provider_type)

        return provider

    async def delete_provider(self, db: AsyncSession, provider_id: str) -> bool:
        """Delete an identity provider.

        Args:
            db: Database session
            provider_id: Provider ID

        Returns:
            True if deleted, False if not found
        """
        result = await db.execute(
            select(IdentityProvider).where(IdentityProvider.id == provider_id)
        )
        provider = result.scalar_one_or_none()

        if not provider:
            return False

        provider_type = provider.provider_type
        await db.delete(provider)
        await db.flush()

        # Clear cache
        self._clear_cache(provider_type)

        return True

    def _clear_cache(self, provider_type: str):
        """Clear cached provider instances for a type."""
        keys_to_delete = [k for k in self._cache if k.startswith(f"{provider_type}:")]
        for key in keys_to_delete:
            del self._cache[key]

    def clear_all_cache(self):
        """Clear all cached provider instances."""
        self._cache.clear()


# Global registry instance
auth_provider_registry = AuthProviderRegistry()
