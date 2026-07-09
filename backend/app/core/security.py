"""Security utilities: JWT, password hashing, and authentication dependencies."""

import asyncio
import base64
import hashlib
from loguru import logger
import os
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import bcrypt
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from jwt import ExpiredSignatureError, PyJWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import get_db
from app.models.user import User

settings = get_settings()

# Application-level salt for AES key derivation. This is NOT a secret --
# it prevents rainbow-table attacks on the key-derivation step.
# If rotated, re-encrypt all ciphertexts and bump _CIPHERTEXT_MAGIC.
_APP_KEY_SALT = b"clawith::aes256::salt-v1"
_PBKDF2_ITERATIONS = 100_000
_CIPHERTEXT_MAGIC = b"\x01\xca\xfe"  # 3-byte magic for PBKDF2 ciphertext (1/16M false-positive vs 1-byte's 1/256)

# ── JWT 过期风暴检测 (按 IP 聚合，防止日志噪音淹没真实问题) ──
# 同一个 IP 在 60s 内连续发送过期 token → 判定为"重连风暴"
_EXPIRED_WINDOW_SEC = 60
_EXPIRED_STORM_THRESHOLD = 5  # 60s 内 5 次过期 → 拒绝该 IP 后续连接
_expired_ip_window: dict[str, list[float]] = defaultdict(list)
_last_expired_ip_cleanup = 0.0

# Bearer token scheme
security = HTTPBearer(auto_error=False)


def _derive_key_v1(key: str) -> bytes:
    """Derive a 32-byte AES-256 key via PBKDF2-HMAC-SHA256 with application salt."""
    return hashlib.pbkdf2_hmac("sha256", key.encode("utf-8"), _APP_KEY_SALT, _PBKDF2_ITERATIONS, dklen=32)


def _derive_key_legacy(key: str) -> bytes:
    """Legacy single-SHA-256 key derivation (kept for backwards compatibility)."""
    return hashlib.sha256(key.encode("utf-8")).digest()

# Thread pool for CPU-intensive bcrypt operations (avoids blocking the event loop)
_bcrypt_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bcrypt")


def hash_password(password: str) -> str:
    """Hash a password using bcrypt (sync, for use in background tasks)."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash (sync, for use in background tasks)."""
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


async def hash_password_async(password: str) -> str:
    """Hash a password using bcrypt without blocking the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_bcrypt_executor, hash_password, password)


async def verify_password_async(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash without blocking the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_bcrypt_executor, verify_password, plain_password, hashed_password)


def encrypt_data(plaintext: str, key: str) -> str:
    """Encrypt a string using AES-256-CBC with the given key.

    Uses PBKDF2-HMAC-SHA256 (100k iterations) with an application salt for
    key derivation. Ciphertext includes a 3-byte magic prefix so
    decrypt_data can reliably detect the derivation method.

    Returns:
        Base64-encoded encrypted string: 3-byte magic + IV + ciphertext
    """
    if not plaintext:
        return ""

    aes_key = _derive_key_v1(key)

    logger.debug("[SEC] encrypt_data: plaintext_len={}", len(plaintext))
    iv = os.urandom(16)

    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    padded_data = pad(plaintext.encode("utf-8"), AES.block_size)
    encrypted = cipher.encrypt(padded_data)

    # Prepend 3-byte magic so decrypt_data can identify the derivation method reliably
    result = base64.b64encode(_CIPHERTEXT_MAGIC + iv + encrypted).decode("utf-8")
    return result


def decrypt_data(ciphertext: str, key: str) -> str:
    """Decrypt a string encrypted with encrypt_data.

    Auto-detects the key-derivation from the ciphertext header:
    - 3-byte magic \\x01\\xca\\xfe: PBKDF2-HMAC-SHA256 (current)
    - No magic: legacy SHA-256 derivation
    """
    if not ciphertext:
        return ""

    try:
        raw = base64.b64decode(ciphertext)

        if raw[:3] == _CIPHERTEXT_MAGIC:
            aes_key = _derive_key_v1(key)
            iv = raw[3:19]
            encrypted = raw[19:]
        else:
            aes_key = _derive_key_legacy(key)
            iv = raw[:16]
            encrypted = raw[16:]

        cipher = AES.new(aes_key, AES.MODE_CBC, iv)
        padded_data = cipher.decrypt(encrypted)
        plaintext = unpad(padded_data, AES.block_size).decode("utf-8")

        logger.debug("[SEC] decrypt_data: success, plaintext_len={}", len(plaintext))
        return plaintext
    except (ValueError, KeyError) as e:
        logger.debug("[SEC] decrypt_data failed (caller should handle): error={}", e)
        raise ValueError(f"Decryption failed: {e}") from e
    except Exception as e:
        logger.error("[SEC] decrypt_data unexpected error", exc_info=True)
        raise ValueError(f"Decryption failed: {e}") from e




def create_access_token(user_id: str, role: str, expires_delta: timedelta | None = None) -> str:
    """Create a JWT access token."""
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode = {
        "sub": user_id,
        "role": role,
        "exp": expire,
    }
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str, client_ip: str = "") -> dict:
    """Decode and validate a JWT access token.

    附带客户端 IP 用于风暴检测: 同一 IP 60s 内发送 ≥5 次过期 token → 直接拒绝 (429)
    """
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except ExpiredSignatureError:
        # ── JWT 过期风暴检测 ──
        if client_ip:
            _cleanup_expired_window()
            now = time.time()
            _expired_ip_window[client_ip] = [
                t for t in _expired_ip_window[client_ip] if now - t < _EXPIRED_WINDOW_SEC
            ]
            _expired_ip_window[client_ip].append(now)
            count = len(_expired_ip_window[client_ip])

            if count >= _EXPIRED_STORM_THRESHOLD:
                logger.warning(
                    f"[SEC] REJECT expired-token-storm: {count} expired in {_EXPIRED_WINDOW_SEC}s "
                    f"from ip={client_ip} → 429 (possible reconnect loop)"
                )
                raise HTTPException(
                    status_code=429,
                    detail=f"Too many expired token attempts ({count}/{_EXPIRED_WINDOW_SEC}s). "
                           f"请刷新 token 或重新登录。",
                )
            elif count >= 3:
                logger.warning(
                    f"[SEC] JWT expired count={count}/{_EXPIRED_WINDOW_SEC}s ip={client_ip} — approaching storm threshold"
                )
            else:
                logger.warning(f"[SEC] JWT token has expired ip={client_ip}")
        else:
            logger.warning("[SEC] JWT token has expired")

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
        )
    except PyJWTError:
        logger.warning(f"[SEC] JWT decode failed: invalid token ip={client_ip}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )


def _cleanup_expired_window():
    """惰性清理过期窗口条目: 每 120s 一次。"""
    global _last_expired_ip_cleanup
    now = time.time()
    if now - _last_expired_ip_cleanup < 120:
        return
    _last_expired_ip_cleanup = now
    for ip in list(_expired_ip_window.keys()):
        _expired_ip_window[ip] = [t for t in _expired_ip_window[ip] if now - t < _EXPIRED_WINDOW_SEC]
        if not _expired_ip_window[ip]:
            del _expired_ip_window[ip]


def decode_access_token_soft(token: str) -> dict:
    """解码 JWT 但不验证过期时间（#217 修复：用于 refresh 端点）。

    仅验证签名和格式正确性，容忍 exp 已过期的 token。
    """
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_exp": False},
        )
        return payload
    except PyJWTError:
        logger.warning("[SEC] JWT soft-decode failed: invalid token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )




def is_refresh_within_grace(payload: dict) -> bool:
    """判断 JWT 是否在 refresh 宽限窗口内（exp 过期后仍可续期）。"""
    exp = payload.get("exp")
    if exp is None:
        return False
    grace_seconds = settings.JWT_REFRESH_GRACE_DAYS * 86400
    now = datetime.now(timezone.utc).timestamp()
    return now <= float(exp) + grace_seconds


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Get current user from JWT Bearer token (cw- API Key support removed)."""
    from app.models.user import User

    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    # Only JWT Bearer authentication remains
    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
        )

    result = await db.execute(
        select(User).where(User.id == uuid.UUID(str(user_id))).options(selectinload(User.identity))
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive",
        )
    return user


async def get_authenticated_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
):
    """Dependency to get the current authenticated user (even if not active yet)."""

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)).options(selectinload(User.identity)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def verify_api_key_or_token(token: str | None) -> uuid.UUID:
    """Authenticate thin clients via JWT token (cw- API Key support removed).

    #217 修复：区分 token_expired 和 invalid token，
    让 IDE 插件收到 token_expired 后可以尝试调用 /api/auth/refresh 换新 token。
    """
    from app.database import async_session

    if not token or not str(token).strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing token",
        )
    raw = str(token).strip()

    # 先尝试验证完整 token（含过期检查）
    try:
        payload = jwt.decode(raw, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except ExpiredSignatureError:
        # #217：过期 token 返回特定错误码，客户端可尝试 refresh
        try:
            payload = jwt.decode(
                raw, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM], options={"verify_exp": False}
            )
        except PyJWTError:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        user_id = payload.get("sub")
        if user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="token_expired",
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except PyJWTError:
        logger.warning("[SEC] JWT decode failed: invalid token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    async with async_session() as db:
        result = await db.execute(
            select(User).where(User.id == uuid.UUID(str(user_id))).options(selectinload(User.identity))
        )
        user = result.scalar_one_or_none()
        if not user or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or inactive",
            )
        return user.id


async def get_current_admin(current_user=Depends(get_current_user)):
    """Dependency to require admin role (platform_admin or org_admin)."""
    identity_is_platform_admin = bool(getattr(getattr(current_user, "identity", None), "is_platform_admin", False))
    if current_user.role not in ("platform_admin", "org_admin") and not identity_is_platform_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


# Role hierarchy: higher index = more privileges
ROLE_HIERARCHY = ["member", "agent_admin", "org_admin", "platform_admin"]


def require_role(*allowed_roles: str):
    """Factory to create a dependency that checks if the user has one of the allowed roles.

    Usage:
        @router.post("/", dependencies=[Depends(require_role("org_admin", "platform_admin"))])
        async def my_endpoint(...):
    """

    async def _check(current_user=Depends(get_current_user)):
        identity_is_platform_admin = bool(getattr(getattr(current_user, "identity", None), "is_platform_admin", False))
        if current_user.role not in allowed_roles and not (
            "platform_admin" in allowed_roles and identity_is_platform_admin
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要以下角色之一: {', '.join(allowed_roles)}",
            )
        return current_user

    return _check
