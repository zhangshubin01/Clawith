"""Channel credential encryption at rest.

Uses Fernet (AES-128-CBC + HMAC-SHA256) symmetric encryption.
Encryption key from environment variable CHANNEL_CREDENTIAL_KEY.
If CHANNEL_CREDENTIAL_KEY is not set, credentials are stored in plaintext
with a warning log (compatibility mode for existing deployments).
"""

import os
import base64
from loguru import logger

try:
    from cryptography.fernet import Fernet, InvalidToken as _InvalidToken
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False
    Fernet = None  # type: ignore
    _InvalidToken = Exception  # type: ignore


_ENV_KEY = os.environ.get("CHANNEL_CREDENTIAL_KEY", "").strip()
_fernet: "Fernet | None" = None

if _ENV_KEY and _HAS_CRYPTO:
    try:
        # Fernet 需要 32 字节 url-safe base64 编码的密钥
        key_bytes = base64.urlsafe_b64encode(_ENV_KEY.encode().ljust(32, b'\0')[:32])
        _fernet = Fernet(key_bytes)
        logger.info("[CredentialCrypto] Fernet encryption enabled")
    except Exception as e:
        logger.warning(f"[CredentialCrypto] Failed to initialize Fernet: {e}")
elif not _ENV_KEY:
    logger.warning("[CredentialCrypto] CHANNEL_CREDENTIAL_KEY not set — credentials stored in plaintext")
elif not _HAS_CRYPTO:
    logger.warning("[CredentialCrypto] cryptography package not installed — credentials stored in plaintext")


def encrypt_credential(plaintext: str | None) -> str | None:
    """Encrypt a credential string. Returns plaintext if encryption is unavailable."""
    if not plaintext:
        return plaintext
    if _fernet is None:
        return plaintext  # 兼容模式：明文存储
    return "fernet:" + _fernet.encrypt(plaintext.encode()).decode()


def decrypt_credential(ciphertext: str | None) -> str | None:
    """Decrypt a credential string. Returns plaintext if not encrypted."""
    if not ciphertext:
        return ciphertext
    if not ciphertext.startswith("fernet:"):
        return ciphertext  # 旧数据：已是明文
    if _fernet is None:
        # 加密密钥不可用但数据已加密 — 无法解密
        logger.error("[CredentialCrypto] Encrypted credential found but CHANNEL_CREDENTIAL_KEY is not configured")
        return None
    try:
        return _fernet.decrypt(ciphertext[7:].encode()).decode()
    except Exception:
        logger.error("[CredentialCrypto] Failed to decrypt credential — wrong key or corrupted data")
        return None


def is_encrypted(value: str | None) -> bool:
    """Check if a value is encrypted (starts with 'fernet:')."""
    return bool(value and value.startswith("fernet:"))
