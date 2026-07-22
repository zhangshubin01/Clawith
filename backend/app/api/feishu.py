"""Feishu OAuth and Channel API routes."""

import base64
import hashlib
import json
import re
import time as _time
import uuid
from collections import OrderedDict, defaultdict

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.database import async_session as _async_session, get_db
from app.config import get_settings
from app.models.channel_config import ChannelConfig
from app.models.user import User

_settings = get_settings()
from app.schemas.schemas import ChannelConfigCreate, ChannelConfigOut, TokenResponse, UserOut
from app.services.agent_runtime.channel_chat import (
    channel_message_id,
    enqueue_channel_chat_runtime,
)
from app.services.agent_runtime.chat_intake import ChatRuntimeIntake
from app.services.feishu_service import feishu_service
from app.services.storage import store_agent_upload


class DedupStore:
    """LRU 去重存储，带 TTL 和 FIFO 驱逐。替代旧的 _event_dedup set。"""

    def __init__(self, ttl_seconds: int = 30 * 60, max_size: int = 1000):
        self._ttl = ttl_seconds
        self._max = max_size
        self._store: OrderedDict[str, float] = OrderedDict()

    def is_duplicate(self, event_id: str) -> bool:
        self._expire()
        return event_id in self._store

    def mark_seen(self, event_id: str) -> None:
        self._expire()
        if event_id in self._store:
            self._store.move_to_end(event_id)
            return
        if len(self._store) >= self._max:
            self._store.popitem(last=False)
        self._store[event_id] = _time.monotonic()
        self._store.move_to_end(event_id)

    def _expire(self) -> None:
        cutoff = _time.monotonic() - self._ttl
        # 遍历所有条目驱逐过期项。不能仅从左侧弹出，因为 mark_seen 的 move_to_end
        # 会将最近访问的旧条目移到右侧，导致左侧新条目被误驱逐。
        expired = [k for k, v in self._store.items() if v < cutoff]
        for k in expired:
            del self._store[k]


class ProcessingLock:
    """防止同一消息被并发处理。asyncio 单线程安全。5 分钟 TTL 防死锁。"""

    def __init__(self, ttl_seconds: int = 300):
        self._ttl = ttl_seconds
        self._locks: dict[str, float] = {}

    def try_acquire(self, event_id: str) -> bool:
        self._sweep()
        if event_id in self._locks:
            return False
        self._locks[event_id] = _time.monotonic()
        return True

    def release(self, event_id: str) -> None:
        self._locks.pop(event_id, None)

    def _sweep(self) -> None:
        cutoff = _time.monotonic() - self._ttl
        stale = [k for k, v in self._locks.items() if v < cutoff]
        for k in stale:
            self._locks.pop(k, None)


class PerAgentRateLimiter:
    """滑动窗口速率限制器。"""

    def __init__(self, max_per_second: int = 10):
        self._max = max_per_second
        self._windows: dict[str, list[float]] = defaultdict(list)

    def check(self, agent_id: str) -> bool:
        now = _time.monotonic()
        window = self._windows[agent_id]
        while window and window[0] < now - 1.0:
            window.pop(0)
        if not window:
            del self._windows[agent_id]
            return True
        if len(window) >= self._max:
            return False
        window.append(now)
        return True


_event_dedup = DedupStore()
_event_lock = ProcessingLock()
_ratelimiter = PerAgentRateLimiter()


router = APIRouter(tags=["feishu"])

_USER_RESOLUTION_ERROR_TIP = (
    "抱歉，我暂时无法稳定识别你的飞书账号，已停止本次处理以避免重复创建账号。"
    "请稍后重试，或联系管理员检查飞书 Contact API 权限。"
)


# ─── OAuth ──────────────────────────────────────────────

@router.get("/auth/feishu/callback")
@router.post("/auth/feishu/callback", response_model=TokenResponse)
async def feishu_oauth_callback(
    code: str, 
    state: str = None, 
    db: AsyncSession = Depends(get_db)
):
    """Handle Feishu OAuth callback — exchange code for user session."""
    # Parse state if it's a UUID (session ID) or other context
    from app.models.identity import SSOScanSession
    tenant_id = None
    if state:
        try:
            sid = uuid.UUID(state)
            s_res = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
            session = s_res.scalar_one_or_none()
            if session:
                tenant_id = session.tenant_id
        except (ValueError, AttributeError):
            pass

    try:
        # Use FeishuAuthProvider instead of legacy feishu_service
        from app.services.auth_provider import FeishuAuthProvider
        from app.models.identity import IdentityProvider
        from app.config import get_settings

        # Get Feishu credentials from settings
        settings = get_settings()
        feishu_config = {
            "app_id": settings.FEISHU_APP_ID,
            "app_secret": settings.FEISHU_APP_SECRET,
        }

        # Get or create provider via auth provider
        provider = None
        if tenant_id:
            result = await db.execute(
                select(IdentityProvider).where(
                    IdentityProvider.provider_type == "feishu",
                    IdentityProvider.tenant_id == tenant_id
                )
            )
            provider = result.scalar_one_or_none()

        auth_provider = FeishuAuthProvider(provider=provider, config=feishu_config)

        # Ensure provider exists (will create if not)
        await auth_provider._ensure_provider(db, tenant_id)
        provider = auth_provider.provider

        # Exchange code for user info
        token_data = await auth_provider.exchange_code_for_token(code)
        access_token = token_data.get("access_token", "")
        user_info = await auth_provider.get_user_info(access_token)

        # Find or create user
        user, is_new = await auth_provider.find_or_create_user(db, user_info, tenant_id=tenant_id)

        # Generate JWT token
        from app.core.security import create_access_token
        token = create_access_token(str(user.id), user.role)

    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Feishu auth failed: {e}")

    # If this is an SSO session, store result and redirect to frontend completion
    if state:
        try:
            sid = uuid.UUID(state)
            s_res = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
            session = s_res.scalar_one_or_none()
            if session:
                session.status = "authorized"
                session.provider_type = "feishu"
                session.user_id = user.id
                session.access_token = token
                session.error_msg = None
                await db.commit()
                return HTMLResponse(
                    f"""<html><head><meta charset="utf-8" /></head>
                    <body style="font-family: sans-serif; padding: 24px;">
                        <div>SSO login successful. Redirecting...</div>
                        <script>window.location.href = "/sso/entry?sid={sid}&complete=1";</script>
                    </body></html>"""
                )
        except Exception as e:
            logger.exception("Failed to update SSO session (feishu) %s", e)

    return TokenResponse(access_token=token, user=UserOut.model_validate(user))


# ─── Channel Config (per-agent Feishu bot) ──────────────

@router.post("/agents/{agent_id}/channel", response_model=ChannelConfigOut, status_code=status.HTTP_201_CREATED)
async def configure_channel(
    agent_id: uuid.UUID,
    data: ChannelConfigCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Configure Feishu bot credentials for a digital employee (wizard step 5)."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    # Check existing
    result = await db.execute(select(ChannelConfig).where(
        ChannelConfig.agent_id == agent_id,
        ChannelConfig.channel_type == "feishu",
    ))
    existing = result.scalar_one_or_none()
    if existing:
        existing.app_id = data.app_id
        existing.app_secret = data.app_secret
        existing.encrypt_key = data.encrypt_key
        existing.verification_token = data.verification_token
        existing.extra_config = data.extra_config or {}
        existing.is_configured = True
        await db.flush()
        
        # Start/Stop WS client in background
        from app.services.feishu_ws import feishu_ws_manager
        import asyncio
        mode = existing.extra_config.get("connection_mode", "webhook")
        if mode == "websocket":
            asyncio.create_task(feishu_ws_manager.start_client(agent_id, existing.app_id, existing.app_secret))
        else:
            asyncio.create_task(feishu_ws_manager.stop_client(agent_id))

        # Phase 1: 通知 FeishuChannelManager 热更新
        from app.services.feishu_channel_manager import feishu_channel_manager
        asyncio.create_task(feishu_channel_manager.reload(agent_id))

        return ChannelConfigOut.model_validate(existing)

    config = ChannelConfig(
        agent_id=agent_id,
        channel_type=data.channel_type,
        app_id=data.app_id,
        app_secret=data.app_secret,
        encrypt_key=data.encrypt_key,
        verification_token=data.verification_token,
        extra_config=data.extra_config or {},
        is_configured=True,
    )
    db.add(config)
    await db.flush()

    # Start WS client in background
    from app.services.feishu_ws import feishu_ws_manager
    import asyncio
    mode = config.extra_config.get("connection_mode", "webhook")
    if mode == "websocket":
        asyncio.create_task(feishu_ws_manager.start_client(agent_id, config.app_id, config.app_secret))

    # Phase 1: 通知 FeishuChannelManager 热更新
    from app.services.feishu_channel_manager import feishu_channel_manager
    asyncio.create_task(feishu_channel_manager.reload(agent_id))

    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/channel", response_model=ChannelConfigOut)
async def get_channel_config(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get Feishu channel configuration for an agent."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(select(ChannelConfig).where(
        ChannelConfig.agent_id == agent_id,
        ChannelConfig.channel_type == "feishu",
    ))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Channel not configured")
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/channel/webhook-url")
async def get_webhook_url(agent_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """Get the webhook URL for this agent's Feishu bot."""
    from app.services.platform_service import platform_service
    public_base = await platform_service.get_public_base_url(db, request)
    return {"webhook_url": f"{public_base}/api/channel/feishu/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/channel", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel_config(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove Feishu bot configuration for an agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")
    result = await db.execute(select(ChannelConfig).where(
        ChannelConfig.agent_id == agent_id,
        ChannelConfig.channel_type == "feishu",
    ))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Channel not configured")
    await db.delete(config)

    # 停止 WS 客户端（如果存在）
    from app.services.feishu_ws import feishu_ws_manager
    await feishu_ws_manager.stop_client(agent_id)


# ─── Feishu Event Webhook ───────────────────────────────


async def _resolve_feishu_sender(
    db: AsyncSession,
    *,
    agent,
    config: ChannelConfig,
    sender_open_id: str,
    sender_user_id: str,
):
    """Resolve the stable tenant user while preserving Feishu identifiers."""
    from app.services.channel_user_service import channel_user_service
    from app.services.feishu_service import feishu_service

    resolved_user_id = sender_user_id.strip()
    extra_info: dict = {
        "open_id": sender_open_id,
        "external_id": resolved_user_id or None,
    }
    try:
        app_token = await feishu_service.get_app_access_token(config.app_id, config.app_secret)
        if app_token:
            client = feishu_service._get_client()
            user_response = await client.get(
                f"{_settings.FEISHU_DOMAIN}/open-apis/contact/v3/users/{sender_open_id}",
                params={"user_id_type": "open_id"},
                headers={"Authorization": f"Bearer {app_token}"},
            )
            payload = user_response.json()
            if payload.get("code") == 0:
                user_info = payload.get("data", {}).get("user", {})
                resolved_user_id = user_info.get("user_id") or resolved_user_id
                raw_avatar = user_info.get("avatar")
                avatar_url = (
                    raw_avatar.get("avatar_240")
                    or raw_avatar.get("avatar_640")
                    or raw_avatar.get("avatar_origin")
                    or ""
                    if isinstance(raw_avatar, dict)
                    else raw_avatar or ""
                )
                extra_info = {
                    "name": user_info.get("name"),
                    "email": user_info.get("email")
                    or user_info.get("enterprise_email"),
                    "mobile": user_info.get("mobile"),
                    "avatar_url": avatar_url,
                    "external_id": resolved_user_id or None,
                    "unionid": user_info.get("union_id"),
                    "open_id": sender_open_id,
                }
    except Exception as exc:
        logger.warning(f"[Feishu] Sender enrichment failed: {type(exc).__name__}")

    return await channel_user_service.resolve_channel_user(
        db=db,
        agent=agent,
        channel_type="feishu",
        external_user_id=resolved_user_id or None,
        extra_info=extra_info,
    )


async def _accept_feishu_runtime_message(
    *,
    agent_id: uuid.UUID,
    config: ChannelConfig,
    sender_open_id: str,
    sender_user_id: str,
    chat_type: str,
    chat_id: str,
    content: str,
    display_content: str,
    external_event_id: str | None,
) -> ChatRuntimeIntake:
    """Persist a Feishu message and Runtime Command before acknowledging it."""
    from app.models.agent import Agent
    from app.services.channel_session import find_or_create_channel_session

    async with _async_session() as db:
        agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = agent_result.scalar_one_or_none()
        if agent is None:
            raise RuntimeError(f"Feishu Agent {agent_id} not found")
        user = await _resolve_feishu_sender(
            db,
            agent=agent,
            config=config,
            sender_open_id=sender_open_id,
            sender_user_id=sender_user_id,
        )
        is_group = chat_type == "group" and bool(chat_id)
        stable_sender = sender_user_id or sender_open_id
        external_conv_id = (
            f"feishu_group_{chat_id}" if is_group else f"feishu_p2p_{stable_sender}"
        )
        session = await find_or_create_channel_session(
            db=db,
            agent_id=agent_id,
            user_id=agent.creator_id if is_group else user.id,
            external_conv_id=external_conv_id,
            source_channel="feishu",
            first_message_title=display_content or content,
            is_group=is_group,
            group_name=f"Feishu Group {chat_id[:8]}" if is_group else None,
            created_by_user_id=user.id,
        )
        _, model, _ = await _load_agent_and_model(db, agent_id)
        sender_name = (user.display_name or "").strip()
        executable_content = (
            f"[发送者: {sender_name}] {content}" if sender_name else content
        )
        intake = await enqueue_channel_chat_runtime(
            db,
            agent=agent,
            user=user,
            session=session,
            model=model,
            content=executable_content,
            display_content=display_content,
            source_channel="feishu",
            channel_delivery_target={
                "receive_id": chat_id if is_group else sender_open_id,
                "receive_id_type": "chat_id" if is_group else "open_id",
            },
            message_id=channel_message_id(
                agent_id,
                "feishu",
                external_event_id,
            ),
        )
        await db.commit()
    return intake


@router.post("/channel/feishu/{agent_id}/webhook")
async def feishu_event_webhook(
    agent_id: uuid.UUID,
    request: Request,
):
    """Handle Feishu event callback via lark-channel-sdk."""
    from app.services.feishu_channel_manager import feishu_channel_manager

    # Phase 0(i): 速率限制
    if not _ratelimiter.check(str(agent_id)):
        return JSONResponse(status_code=429, content={"code": -1, "msg": "too many requests"})

    channel = feishu_channel_manager.get(agent_id)
    if channel is not None:
        try:
            # SDK 路径：handle_webhook_request 处理解密+验签+challenge+事件路由
            status_code, body_bytes = await channel.handle_webhook_request(
                headers=dict(request.headers),
                body=await request.body(),
            )
            return Response(status_code=status_code, content=body_bytes, media_type="application/json")
        except Exception:
            logger.warning("[Feishu] SDK 路径异常，回退降级路径", exc_info=True)

    # 降级路径：Channel 未初始化或 SDK 异常时使用
    body_bytes = await request.body()

    # Phase 0(a): AES-CBC 解密
    async with _async_session() as db:
        result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == "feishu",
            )
        )
        config = result.scalar_one_or_none()

    encrypt_key = config.encrypt_key if config else None

    if encrypt_key:
        try:
            body_dict = json.loads(body_bytes)
            encrypted = body_dict.get("encrypt")
            if encrypted:
                key = hashlib.sha256(encrypt_key.encode()).digest()
                raw = base64.b64decode(encrypted)
                iv = raw[:16]
                ciphertext = raw[16:]
                cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
                decryptor = cipher.decryptor()
                decrypted = decryptor.update(ciphertext) + decryptor.finalize()
                pad_len = decrypted[-1]
                body_bytes = decrypted[:-pad_len]
        except Exception:
            logger.warning("[Feishu] Webhook AES 解密失败，拒绝处理")
            return JSONResponse(status_code=400, content={"code": -1, "msg": "decrypt failed"})

    body = json.loads(body_bytes)

    if "challenge" in body:
        return {"challenge": body["challenge"]}

    return await process_feishu_event(agent_id, body)


async def process_feishu_event(agent_id: uuid.UUID, body: dict):
    """Accept Feishu events durably and defer only provider result delivery."""
    logger.info(f"[Feishu] Event processing for {agent_id}: event_type={body.get('header', {}).get('event_type', 'N/A')}")

    # Deduplicate — Feishu retries on slow responses
    event_id = body.get("header", {}).get("event_id", "")
    if _event_dedup.is_duplicate(event_id):
        return {"code": 0, "msg": "already processed"}
    if not _event_lock.try_acquire(event_id):
        return {"code": 0, "msg": "already processing"}

    try:
        _event_dedup.mark_seen(event_id)

        # Load channel credentials before parsing the provider event.
        async with _async_session() as db:
            result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "feishu",
                )
            )
            config = result.scalar_one_or_none()
        if not config:
            return {"code": 1, "msg": "Channel not found"}

        # Handle events
        event = body.get("event", {})
        event_type = body.get("header", {}).get("event_type", "")

        if event_type == "im.message.receive_v1":
            message = event.get("message", {})
            sender = event.get("sender", {}).get("sender_id", {})
            sender_open_id = sender.get("open_id", "")
            sender_user_id_from_event = sender.get("user_id", "")  # tenant-stable ID, available directly in event body
            msg_type = message.get("message_type", "text")
            chat_type = message.get("chat_type", "p2p")  # p2p or group
            chat_id = message.get("chat_id", "")

            logger.info(f"[Feishu] Received {msg_type} message, chat_type={chat_type}, open_id={sender_open_id!r}, user_id_from_event={sender_user_id_from_event!r}")

            # ── Normalize post (rich text) → extract text + schedule image downloads ──
            if msg_type == "post":
                _post_body = json.loads(message.get("content", "{}"))
                # Feishu post content: {"title": "...", "content": [[{"tag":"text","text":"..."},...],...]}
                # The content may be nested under a locale key like "zh_cn"
                _paragraphs = _post_body.get("content", [])
                if not _paragraphs:
                    # Try locale keys (zh_cn, en_us, etc.)
                    for _locale_key, _locale_val in _post_body.items():
                        if isinstance(_locale_val, dict) and "content" in _locale_val:
                            _paragraphs = _locale_val["content"]
                            break
                _text_parts = []
                _post_image_keys = []
                for _para in _paragraphs:
                    _line_parts = []
                    for _elem in _para:
                        _tag = _elem.get("tag")
                        if _tag == "text":
                            _line_parts.append(_elem.get("text", ""))
                        elif _tag == "a":
                            _href = _elem.get("href", "")
                            _link_text = _elem.get("text", "")
                            _line_parts.append(f"{_link_text} ({_href})" if _href else _link_text)
                        elif _tag == "img":
                            _ik = _elem.get("image_key", "")
                            if _ik:
                                _post_image_keys.append(_ik)
                    if _line_parts:
                        _text_parts.append("".join(_line_parts))
                _extracted_text = "\n".join(_text_parts).strip()
                # Download images and embed as base64 for vision-capable models
                _image_markers = []
                if _post_image_keys:
                    _msg_id = message.get("message_id", "")
                    for _ik in _post_image_keys:
                        try:
                            _img_bytes = await feishu_service.download_message_resource(
                                config.app_id, config.app_secret, _msg_id, _ik, "image"
                            )
                            _, _workspace_path, _save_path = await store_agent_upload(
                                agent_id,
                                f"image_{_ik[-8:]}.jpg",
                                _img_bytes,
                                content_type="image/jpeg",
                            )
                            logger.info(f"[Feishu] Saved post image to {_workspace_path} ({len(_img_bytes)} bytes)")
                            # Embed as base64 marker for vision models
                            _b64_data = base64.b64encode(_img_bytes).decode("ascii")
                            _image_markers.append(f"[image_data:data:image/jpeg;base64,{_b64_data}]")
                        except Exception as _dl_err:
                            logger.error(f"[Feishu] Failed to download post image {_ik}: {_dl_err}")
                # Build final text with embedded images
                if not _extracted_text and _image_markers:
                    _extracted_text = "[用户发送了图片，请看图片内容]"
                _final_content = _extracted_text
                if _image_markers:
                    _final_content += "\n" + "\n".join(_image_markers)
                # Rewrite as text message so existing handler processes it
                message["content"] = json.dumps({"text": _final_content})
                msg_type = "text"
                logger.info(f"[Feishu] Normalized post → text='{_extracted_text[:100]}', images={len(_image_markers)}")

            if msg_type in ("file", "image"):
                attachment = await _accept_feishu_file_runtime(
                    agent_id=agent_id,
                    config=config,
                    message=message,
                    sender_open_id=sender_open_id,
                    sender_user_id=sender_user_id_from_event,
                    chat_type=chat_type,
                    chat_id=chat_id,
                    external_event_id=event_id or message.get("message_id"),
                )
                if attachment is not None:
                    pass  # dedup handled by outer try/finally
                return {"code": 0, "msg": "ok"}

            if msg_type != "text":
                return {"code": 0, "msg": "unsupported message type"}

            content = json.loads(message.get("content", "{}"))
            user_text = re.sub(r"@_user_\d+", "", content.get("text", "")).strip()
            if not user_text:
                return {"code": 0, "msg": "empty message after stripping mentions"}

            display_content = re.sub(
                r"\[image_data:data:image/[^;]+;base64,[A-Za-z0-9+/=]+\]",
                "",
                user_text,
            ).strip()
            if not display_content and "[image_data:" in user_text:
                display_content = "[图片]"

            try:
                await _accept_feishu_runtime_message(
                    agent_id=agent_id,
                    config=config,
                    sender_open_id=sender_open_id,
                    sender_user_id=sender_user_id_from_event,
                    chat_type=chat_type,
                    chat_id=chat_id,
                    content=user_text,
                    display_content=display_content,
                    external_event_id=event_id or message.get("message_id"),
                )
            except Exception as exc:
                from app.services.channel_user_service import ChannelUserResolutionError

                if not isinstance(exc, ChannelUserResolutionError):
                    raise
                logger.warning(f"[Feishu] Sender resolution refused: {exc}")
                reply_target = chat_id if chat_type == "group" else sender_open_id
                receive_id_type = "chat_id" if chat_type == "group" else "open_id"
                await feishu_service.send_message(
                    config.app_id,
                    config.app_secret,
                    reply_target,
                    "text",
                    json.dumps({"text": _USER_RESOLUTION_ERROR_TIP}),
                    receive_id_type=receive_id_type,
                )
                return {"code": 0, "msg": "user_resolution_skipped"}

            return {"code": 0, "msg": "ok"}
        return {"code": 0, "msg": "ok"}
    finally:
        _event_lock.release(event_id)


async def _accept_feishu_file_runtime(
    *,
    agent_id: uuid.UUID,
    config: ChannelConfig,
    message: dict,
    sender_open_id: str,
    sender_user_id: str,
    chat_type: str,
    chat_id: str,
    external_event_id: str | None,
) -> ChatRuntimeIntake | None:
    """Download a Feishu resource, then durably attach it to the Runtime."""
    message_type = message.get("message_type", "file")
    provider_message_id = message.get("message_id", "")
    content = json.loads(message.get("content", "{}"))
    if message_type == "image":
        file_key = content.get("image_key", "")
        filename = f"image_{file_key[-8:]}.jpg" if file_key else "image.jpg"
        resource_type = "image"
    else:
        file_key = content.get("file_key", "")
        filename = content.get("file_name") or f"file_{file_key[-8:]}.bin"
        resource_type = "file"
    if not file_key:
        logger.warning(f"[Feishu] No file_key in {message_type} message")
        return None

    try:
        file_bytes = await feishu_service.download_message_resource(
            config.app_id,
            config.app_secret,
            provider_message_id,
            file_key,
            resource_type,
        )
        _, workspace_path, _ = await store_agent_upload(
            agent_id,
            filename,
            file_bytes,
            content_type="image/jpeg" if message_type == "image" else None,
        )
    except Exception as exc:
        logger.error(f"[Feishu] Failed to download {message_type}: {exc}")
        reply_target = chat_id if chat_type == "group" else sender_open_id
        receive_id_type = "chat_id" if chat_type == "group" else "open_id"
        await feishu_service.send_message(
            config.app_id,
            config.app_secret,
            reply_target,
            "text",
            json.dumps(
                {
                    "text": (
                        "抱歉，文件下载失败。请检查机器人是否已获得 "
                        "im:resource 权限并重新发布应用版本。"
                    )
                }
            ),
            receive_id_type=receive_id_type,
        )
        return None

    display_content = f"[file:{filename}]"
    file_hint = (
        f"[系统提示：用户上传的文件已保存到工作区 {workspace_path}。"
        "需要读取内容时请直接使用 read_document。]"
    )
    if message_type == "image":
        image_data = base64.b64encode(file_bytes).decode("ascii")
        executable_content = (
            "[用户发送了图片]\n"
            f"[image_data:data:image/jpeg;base64,{image_data}]\n"
            f"{file_hint}"
        )
    else:
        executable_content = f"{display_content}\n{file_hint}"

    try:
        return await _accept_feishu_runtime_message(
            agent_id=agent_id,
            config=config,
            sender_open_id=sender_open_id,
            sender_user_id=sender_user_id,
            chat_type=chat_type,
            chat_id=chat_id,
            content=executable_content,
            display_content=display_content,
            external_event_id=external_event_id or provider_message_id,
        )
    except Exception as exc:
        from app.services.channel_user_service import ChannelUserResolutionError

        if not isinstance(exc, ChannelUserResolutionError):
            raise
        logger.warning(f"[Feishu] File sender resolution refused: {exc}")
        reply_target = chat_id if chat_type == "group" else sender_open_id
        receive_id_type = "chat_id" if chat_type == "group" else "open_id"
        await feishu_service.send_message(
            config.app_id,
            config.app_secret,
            reply_target,
            "text",
            json.dumps({"text": _USER_RESOLUTION_ERROR_TIP}),
            receive_id_type=receive_id_type,
        )
        return None


async def _load_agent_and_model(
    db: AsyncSession, agent_id: uuid.UUID
):
    """Load agent and LLM model configs in a short DB transaction.

    Returns (agent, model, fallback_model). Caller should extract all needed
    scalar values before closing the session to avoid detached-instance errors.
    """
    from app.models.agent import Agent
    from app.models.llm import LLMModel

    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    if not agent:
        return None, None, None

    model = None
    if agent.primary_model_id:
        model_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.primary_model_id))
        model = model_result.scalar_one_or_none()
        if model and not model.enabled:
            logger.info(f"[Channel] Primary model {model.model} is disabled, skipping")
            model = None

    fallback_model = None
    if agent.fallback_model_id:
        fb_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.fallback_model_id))
        fallback_model = fb_result.scalar_one_or_none()
        if fallback_model and not fallback_model.enabled:
            logger.info(f"[Channel] Fallback model {fallback_model.model} is disabled, skipping")
            fallback_model = None

    if not model and fallback_model:
        model = fallback_model
        fallback_model = None
        logger.warning(f"[Channel] Primary model unavailable, using fallback: {model.model}")

    return agent, model, fallback_model
