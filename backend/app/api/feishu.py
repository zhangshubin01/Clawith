"""Feishu OAuth and Channel API routes."""

import asyncio
import hashlib
import hmac
import json
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from lark_oapi.core.utils import AESCipher
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.database import async_session as _async_session, get_db
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.schemas.schemas import ChannelConfigCreate, ChannelConfigOut, TokenResponse, UserOut
from app.services.agent_runtime.channel_chat import (
    channel_message_id,
    enqueue_channel_chat_runtime,
)
from app.services.agent_runtime.chat_intake import ChatRuntimeIntake
from app.services.feishu_service import feishu_service
from app.services.llm.model_resolution import active_agent_model_candidates
from app.services.storage import store_agent_upload
from app.config import get_settings

_settings = get_settings()
_FEISHU_BASE = _settings.FEISHU_DOMAIN

router = APIRouter(tags=["feishu"])

_FEISHU_GROUP_PASSIVE_INSTRUCTION = (
    "You are passively listening in a Feishu group. A message directly addresses you if it "
    "@mentions you, names you or your Agent name, asks you a question or gives you an "
    "instruction, or explicitly asks you to reply. You must visibly answer every directly "
    "addressed message even when it is outside your usual responsibilities. For messages "
    "that do not directly address you, reply normally only when your responsibilities require "
    "a visible response; otherwise your entire final response must be exactly NO_REPLY, with "
    "no other text. Your final response is automatically delivered to the input Feishu group. "
    "Never call send_channel_message to reply to the current conversation. Use that Tool only "
    "when the user explicitly asks you to send a separate message to another person or group, "
    "and then set cross_session_confirmed=true."
)

_USER_RESOLUTION_ERROR_TIP = (
    "抱歉，我暂时无法稳定识别你的飞书账号，已停止本次处理以避免重复创建账号。"
    "请稍后重试，或联系管理员检查飞书 Contact API 权限。"
)

# 对话内中断指令 — 卡片按钮在长连接(WS)模式下收不到点击回调
# (card.action.trigger 仅走 Webhook)，这是唯一可靠的停止方式。
_FEISHU_INTERRUPT_PHRASES = frozenset(
    {"中断", "停止", "取消", "中断回复", "停止回复", "取消回复", "stop", "cancel", "halt"}
)


async def _cancel_active_feishu_run(
    *,
    db: AsyncSession,
    agent: Any,
    user: Any,
    session: Any,
    config: ChannelConfig,
    chat_type: str,
    chat_id: str,
    sender_open_id: str,
) -> str:
    """Cancel the sender's active Feishu run and confirm in-chat.

    Returns a sentinel string instead of a ``ChatRuntimeIntake`` so the
    caller can distinguish this early-exit from the normal intake flow.
    """
    from app.models.agent_run import AgentRun
    from app.services.agent_runtime.adapter import RuntimeCommandIntake
    from app.services.agent_runtime.contracts import CancelRunCommand
    from app.services.agent_runtime.card_stream_bridge import get_bridge

    reply_target = chat_id if chat_type == "group" else sender_open_id
    receive_id_type = "chat_id" if chat_type == "group" else "open_id"

    result = await db.execute(
        select(AgentRun).where(
            AgentRun.agent_id == agent.id,
            AgentRun.session_id == session.id,
            AgentRun.lane_held.is_(True),
            AgentRun.source_type == "chat",
        ).limit(1)
    )
    run = result.scalars().first()
    if run is None:
        await feishu_service.send_message(
            config.app_id,
            config.app_secret,
            reply_target,
            "text",
            json.dumps({"text": "当前没有正在执行的任务。"}),
            receive_id_type=receive_id_type,
        )
        return "no_active_run"

    await RuntimeCommandIntake(db).cancel_run(
        CancelRunCommand(
            tenant_id=agent.tenant_id,
            run_id=run.id,
            idempotency_key=f"cancel:feishu:{run.id}",
            reason="cancelled_by_user",
            actor_user_id=user.id,
        )
    )
    await db.commit()

    bridge = get_bridge(str(run.id))
    if bridge is not None:
        try:
            await bridge.abort("⏹ 回复已中断")
        except Exception:
            logger.exception("[FEISHU-CARD] interrupt_abort_failed run_id={}", run.id)

    await feishu_service.send_message(
        config.app_id,
        config.app_secret,
        reply_target,
        "text",
        json.dumps({"text": "⏹ 已中断当前任务。"}),
        receive_id_type=receive_id_type,
    )
    logger.info("[Feishu] interrupt_cancelled run_id={}", run.id)
    return "interrupted"
_FEISHU_MENTION_PLACEHOLDER_RE = re.compile(r"@_user_\d+")


def _feishu_mention_label(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:100]


def _restore_feishu_text_mentions(text: object, mentions: object) -> str:
    """Restore provider placeholders to visible names before model intake."""
    normalized = text if isinstance(text, str) else ""
    if isinstance(mentions, list):
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            key = mention.get("key")
            name = _feishu_mention_label(mention.get("name"))
            if isinstance(key, str) and key and name:
                normalized = normalized.replace(key, f"@{name}")
    return _FEISHU_MENTION_PLACEHOLDER_RE.sub("", normalized).strip()


def _verify_and_decode_feishu_callback(
    body_bytes: bytes,
    headers: dict[str, str],
    config: ChannelConfig,
) -> dict | None:
    """Authenticate a Feishu callback before any event data is consumed."""
    try:
        envelope = json.loads(body_bytes)
        if not isinstance(envelope, dict):
            return None

        encrypt_key = (config.encrypt_key or "").strip()
        encrypted = envelope.get("encrypt")
        if encrypted:
            if not encrypt_key:
                return None
            payload = json.loads(AESCipher(encrypt_key).decrypt_str(encrypted))
        else:
            payload = envelope
        if not isinstance(payload, dict):
            return None

        verification_token = (config.verification_token or "").strip()
        actual_token = str((payload.get("header") or {}).get("token") or "")
        if not verification_token or not hmac.compare_digest(actual_token, verification_token):
            return None

        event_type = str((payload.get("header") or {}).get("event_type") or "")
        if encrypt_key and event_type != "url_verification":
            timestamp = headers.get("x-lark-request-timestamp", "")
            nonce = headers.get("x-lark-request-nonce", "")
            signature = headers.get("x-lark-signature", "")
            if not timestamp or not nonce or not signature:
                return None
            expected = hashlib.sha256(
                (timestamp + nonce + encrypt_key).encode() + body_bytes
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return None
        return payload
    except (UnicodeDecodeError, ValueError, TypeError):
        return None


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
        token = create_access_token(str(user.id), user.role, tenant_id=str(user.tenant_id) if user.tenant_id else None)

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

    # 保存前验证凭据有效性（借鉴 DeepThink testFeishuCredentials）
    if data.app_id and data.app_secret:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=8) as _client:
                _resp = await _client.post(
                    f"{_FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": data.app_id, "app_secret": data.app_secret},
                )
                _payload = _resp.json()
                if _resp.status_code >= 400 or _payload.get("code") != 0:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Feishu credential test failed: {_payload.get('msg', 'Unknown error')} (code {_payload.get('code', 'N/A')})",
                    )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Feishu credential test failed: {exc}",
            )

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
            asyncio.create_task(feishu_ws_manager.start_client(agent_id, existing.app_id, existing.app_secret, domain=_FEISHU_BASE))
        else:
            asyncio.create_task(feishu_ws_manager.stop_client(agent_id))
        
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
        asyncio.create_task(feishu_ws_manager.start_client(agent_id, config.app_id, config.app_secret, domain=_FEISHU_BASE))

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
    from app.services.feishu_ws import feishu_ws_manager
    await feishu_ws_manager.stop_client(agent_id)
    await db.delete(config)



# ─── Feishu Event Webhook ───────────────────────────────


async def _resolve_feishu_sender(
    db: AsyncSession,
    *,
    agent,
    config: ChannelConfig,
    sender_open_id: str,
    sender_user_id: str,
):
    """Resolve the stable tenant user while preserving Feishu identifiers.

    fail-closed: 如果 event 中没有 user_id 且 Contact API 调用失败，拒绝解析而非静默降级到仅 open_id。
    借鉴 DeepThink botOpenId fail-closed 语义：信息不足时拒绝，由调用方提示用户，而非创建重复账号。
    """
    from app.services.channel_user_service import channel_user_service, ChannelUserResolutionError

    resolved_user_id = sender_user_id.strip()
    extra_info: dict = {
        "open_id": sender_open_id,
        "external_id": resolved_user_id or None,
    }
    contact_failed = False
    try:
        # 缓存方法：token + 用户信息均按 (app_id, open_id) TTL 缓存，
        # 同用户连续消息不再打 2 个 Contact API RTT。
        user_info = await feishu_service.get_contact_user_cached(
            config.app_id, config.app_secret, sender_open_id,
        )
        if user_info:
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
        else:
            contact_failed = True
    except Exception as exc:
        logger.warning(f"[Feishu] Sender enrichment failed: {exc}")
        contact_failed = True

    # fail-closed: 如果 event 中缺少 user_id 且 Contact API 也调用失败，
    # 拒绝解析以避免仅凭 open_id 创建不准确的用户记录。
    if contact_failed and not resolved_user_id:
        logger.error(
            "[Feishu] Cannot resolve sender: no user_id in event and Contact API failed. "
            "Refusing to proceed with open_id-only resolution to prevent duplicate user creation."
        )
        raise ChannelUserResolutionError(
            "Feishu sender could not be resolved: no user_id in event and Contact API unavailable"
        )

    return await channel_user_service.resolve_channel_user(
        db=db,
        agent=agent,
        channel_type="feishu",
        external_user_id=resolved_user_id or None,
        extra_info=extra_info,
    )


async def _withdraw_card_bridge(
    *,
    bridge,
    pregenerated_run_id: uuid.UUID,
    unregister_bridge,
    reason: str,
) -> None:
    """撤回提前创建的卡片并从注册表移除，失败仅记日志（流程继续）。

    撤回失败只会在聊天里留下一个空的骨架卡片 — 无害；但注册表条目
    必须无论成败都清理，否则残留直至 30 分钟 age-sweep。
    """
    try:
        await bridge.withdraw()
    except Exception:
        logger.exception("[FEISHU-CARD] withdraw_failed reason={}", reason)
    unregister_bridge(str(pregenerated_run_id))


def _fire_early_card(
    *,
    agent,
    config: ChannelConfig,
    chat_type: str,
    chat_id: str,
    sender_open_id: str,
    pregenerated_run_id: uuid.UUID,
):
    """提前创建卡片流式桥并 fire ``start()`` — 与其余 intake 工作并发。

    建卡只依赖 agent_name + receive_id + 凭据（均来自事件与配置）；
    返回 ``(bridge, register_bridge, unregister_bridge)``，调用方负责
    在判定不需要这张卡时走 ``_withdraw_card_bridge``。
    """
    from app.services.agent_runtime.card_stream_bridge import (
        CardStreamBridge,
        register_bridge as _reg_bridge,
        unregister_bridge as _unreg_bridge,
    )
    from app.services.feishu_service import feishu_service as fs
    is_group = chat_type == "group" and bool(chat_id)
    bridge = CardStreamBridge(
        feishu_service=fs,
        app_id=config.app_id or "",
        app_secret=config.app_secret or "",
        receive_id=chat_id if is_group else sender_open_id,
        receive_id_type="chat_id" if is_group else "open_id",
        agent_name=agent.name or str(agent.id),
        run_id=str(pregenerated_run_id),
    )
    card_task = asyncio.create_task(bridge.start())
    card_task.add_done_callback(
        lambda t: (
            logger.error("[FEISHU-CARD] card_task_failed: %s", t.exception())
            if t.exception() else None
        )
    )
    _reg_bridge(str(pregenerated_run_id), bridge)
    logger.info("[FEISHU-CARD] bridge_created run_id={}", pregenerated_run_id)
    return bridge, _reg_bridge, _unreg_bridge


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
    pregenerated_run_id: uuid.UUID | None = None,
    early_card=None,
) -> ChatRuntimeIntake | str:
    """Persist a Feishu message and Runtime Command before acknowledging it.

    Returns ``"interrupted"`` / ``"no_active_run"`` when the message was an
    in-chat interrupt command handled inline (no Runtime Command enqueued).

    ``early_card``: 调用方（文件消息路径）已通过 ``_fire_early_card`` 提前
    创建的 ``(bridge, register_bridge, unregister_bridge)`` — 本函数直接
    复用，不再重复建卡。
    """
    from app.models.agent import Agent
    from app.services.channel_session import find_or_create_channel_session
    from app.services.channel_user_service import ChannelUserResolutionError

    async with _async_session() as db:
        agent_result = await db.execute(
            select(Agent).where(
                Agent.id == agent_id,
                Agent.deleted_at.is_(None),
            )
        )
        agent = agent_result.scalar_one_or_none()
        if agent is None:
            raise RuntimeError(f"Feishu Agent {agent_id} not found")
        is_group = chat_type == "group" and bool(chat_id)
        card_mode = bool(config.app_id and config.app_secret)
        pregenerated_run_id = pregenerated_run_id or uuid.uuid4()
        bridge = None
        register_bridge = None
        unregister_bridge = None
        # 对话内中断指令 — 纯文本判断提前：中断指令不建卡
        # (卡片按钮在长连接模式下收不到点击回调，用户回复指令是唯一可靠停止方式)
        _normalized = re.sub(r"\s+", "", content).strip().lower()
        is_interrupt = _normalized in _FEISHU_INTERRUPT_PHRASES
        # 卡片提前创建 — 与发送者解析/会话创建并发，压缩首帧延迟。
        # 建卡只依赖 agent_name + receive_id + 凭据，全部来自事件与配置；
        # 若随后判定为 resume 会撤回（withdraw）这张提前建的卡片。
        if early_card is not None:
            bridge, register_bridge, unregister_bridge = early_card
        elif card_mode and not is_interrupt:
            bridge, register_bridge, unregister_bridge = _fire_early_card(
                agent=agent,
                config=config,
                chat_type=chat_type,
                chat_id=chat_id,
                sender_open_id=sender_open_id,
                pregenerated_run_id=pregenerated_run_id,
            )
        try:
            user = await _resolve_feishu_sender(
                db,
                agent=agent,
                config=config,
                sender_open_id=sender_open_id,
                sender_user_id=sender_user_id,
            )
        except ChannelUserResolutionError:
            # 发送者解析失败时，提前创建的卡片需要撤回再让错误传播。
            if bridge is not None:
                await _withdraw_card_bridge(
                    bridge=bridge,
                    pregenerated_run_id=pregenerated_run_id,
                    unregister_bridge=unregister_bridge,
                    reason="resolution_failure",
                )
            raise
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
        # 对话内中断指令 — 卡片按钮在长连接模式下收不到点击回调，
        # 用户回复「中断/停止/取消」时直接取消当前活跃 run。
        if is_interrupt:
            return await _cancel_active_feishu_run(
                db=db,
                agent=agent,
                user=user,
                session=session,
                config=config,
                chat_type=chat_type,
                chat_id=chat_id,
                sender_open_id=sender_open_id,
            )
        # 精确 resume 检查 — 会话可用后判断；命中 resume 时撤回提前建的卡片。
        if card_mode and bridge is not None:
            from app.models.agent_run import AgentRun as _AgentRun
            from sqlalchemy import select as _select
            resume_result = await db.execute(
                _select(_AgentRun.id).where(
                    _AgentRun.agent_id == agent_id,
                    _AgentRun.session_id == session.id,
                    _AgentRun.origin_user_id == user.id,
                    _AgentRun.lane_held.is_(True),
                    _AgentRun.source_type == "chat",
                ).limit(1)
            )
            if resume_result.scalar_one_or_none() is not None:
                logger.info(
                    "[FEISHU-CARD] resume_detected — withdrawing pre-created card "
                    "run_id={}", pregenerated_run_id,
                )
                await _withdraw_card_bridge(
                    bridge=bridge,
                    pregenerated_run_id=pregenerated_run_id,
                    unregister_bridge=unregister_bridge,
                    reason="resume",
                )
                bridge = None
                register_bridge = None
        _, model, _ = await _load_agent_and_model(db, agent_id)
        sender_name = (user.display_name or "").strip() or "未知用户"
        sender_identity = " | ".join(
            part
            for part in (
                f"飞书发送者: {sender_name}",
                f"user_id: {sender_user_id.strip()}" if sender_user_id.strip() else "",
                f"open_id: {sender_open_id.strip()}" if sender_open_id.strip() else "",
            )
            if part
        )
        executable_content = f"[{sender_identity}] {content}"
        try:
            intake = await enqueue_channel_chat_runtime(
                db,
                agent=agent,
                user=user,
                session=session,
                model=model,
                content=executable_content,
                display_content=display_content,
                runtime_instruction=(
                    _FEISHU_GROUP_PASSIVE_INSTRUCTION if is_group else ""
                ),
                source_channel="feishu",
                run_id=pregenerated_run_id,
                channel_delivery_target={
                    "receive_id": chat_id if is_group else sender_open_id,
                    "receive_id_type": "chat_id" if is_group else "open_id",
                    **({
                        "_card_config": {
                            "app_id": config.app_id or "",
                            "app_secret": config.app_secret or "",
                        }
                    } if card_mode else {}),
                    **(
                        {"source_message_id": external_event_id.strip()}
                        if is_group and external_event_id and external_event_id.strip()
                        else {}
                    ),
                },
                message_id=channel_message_id(
                    agent_id,
                    "feishu",
                    external_event_id,
                ),
            )
            await db.commit()
        except Exception:
            if bridge is not None:
                try:
                    await asyncio.wait_for(bridge._card_ready.wait(), timeout=15)
                    await bridge.abort("消息处理失败")
                except Exception:
                    logger.exception("[FEISHU-CARD] abort_on_intake_failure_failed")
            raise
        # 验证 run_id 一致（幂等重试场景）— 双键注册
        if bridge is not None:
            actual_run_id = intake.handle.run_id
            if actual_run_id != pregenerated_run_id:
                logger.info(
                    "[FEISHU-CARD] run_id_rebound from={} to={}",
                    pregenerated_run_id, actual_run_id,
                )
                register_bridge(str(actual_run_id), bridge)
    return intake


# Simple in-memory dedup to avoid processing retried events
_processed_events: set[str] = set()


@router.post("/channel/feishu/{agent_id}/webhook")
async def feishu_event_webhook(
    agent_id: uuid.UUID,
    request: Request,
):
    """Handle Feishu event callback for a specific agent's bot."""
    body_bytes = await request.body()
    async with _async_session() as db:
        result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == "feishu",
            )
        )
        config = result.scalar_one_or_none()
    if not config:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    body = _verify_and_decode_feishu_callback(body_bytes, dict(request.headers), config)
    if body is None:
        logger.warning("[Feishu] Rejected unauthenticated callback for {}", agent_id)
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    # Handle verification challenge
    if "challenge" in body:
        return {"challenge": body["challenge"]}

    return await process_feishu_event(agent_id, body)


async def process_feishu_event(agent_id: uuid.UUID, body: dict):
    """Accept Feishu events durably and defer only provider result delivery."""
    logger.info(f"[Feishu] Event processing for {agent_id}: event_type={body.get('header', {}).get('event_type', 'N/A')}")

    # Deduplicate — Feishu retries on slow responses
    # Only mark as processed AFTER successful handling so retries work on crash
    event_id = body.get("header", {}).get("event_id", "")
    if event_id in _processed_events:
        return {"code": 0, "msg": "already processed"}

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
        sender_type = event.get("sender", {}).get("sender_type", "")
        sender_open_id = sender.get("open_id", "")
        sender_user_id_from_event = sender.get("user_id", "")  # tenant-stable ID, available directly in event body
        msg_type = message.get("message_type", "text")
        chat_type = message.get("chat_type", "p2p")  # p2p or group
        chat_id = message.get("chat_id", "")

        if chat_type == "group" and sender_type and sender_type != "user":
            logger.info(
                "[Feishu] Ignoring non-user group message sender_type={}",
                sender_type,
            )
            return {"code": 0, "msg": "non-user group message ignored"}

        logger.info(f"[Feishu] Received {msg_type} message, chat_type={chat_type}, open_id={sender_open_id!r}, user_id_from_event={sender_user_id_from_event!r}")

        # ── Normalize post (rich text) → extract text + schedule image downloads ──
        if msg_type == "post":
            import json as _json_post
            _post_body = _json_post.loads(message.get("content", "{}"))
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
                    elif _tag == "at":
                        _mention_name = _feishu_mention_label(
                            _elem.get("user_name") or _elem.get("name")
                        )
                        if _mention_name:
                            _line_parts.append(f"@{_mention_name}")
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
                import base64 as _b64
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
                        _b64_data = _b64.b64encode(_img_bytes).decode("ascii")
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
            message["content"] = _json_post.dumps({"text": _final_content})
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
                external_event_id=message.get("message_id") or event_id,
            )
            if attachment is not None:
                if event_id:
                    _processed_events.add(event_id)
                    if len(_processed_events) > 1000:
                        _processed_events.clear()
            return {"code": 0, "msg": "ok"}

        if msg_type != "text":
            return {"code": 0, "msg": "unsupported message type"}

        content = json.loads(message.get("content", "{}"))
        user_text = _restore_feishu_text_mentions(
            content.get("text", ""),
            message.get("mentions"),
        )
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
                external_event_id=message.get("message_id") or event_id,
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

        if event_id:
            _processed_events.add(event_id)
            if len(_processed_events) > 1000:
                _processed_events.clear()
        return {"code": 0, "msg": "ok"}
    return {"code": 0, "msg": "ok"}


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
    import base64
    import json

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

    # 卡片提前创建 — 与文件下载并发：建卡只依赖事件与配置，不依赖下载产物
    pregenerated_run_id = uuid.uuid4()
    early_card = None
    if bool(config.app_id and config.app_secret):
        from app.models.agent import Agent as _Agent

        async with _async_session() as _db:
            _agent_result = await _db.execute(
                select(_Agent).where(
                    _Agent.id == agent_id,
                    _Agent.deleted_at.is_(None),
                )
            )
            _agent = _agent_result.scalar_one_or_none()
        if _agent is not None:
            early_card = _fire_early_card(
                agent=_agent,
                config=config,
                chat_type=chat_type,
                chat_id=chat_id,
                sender_open_id=sender_open_id,
                pregenerated_run_id=pregenerated_run_id,
            )

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
        if early_card is not None:
            await _withdraw_card_bridge(
                bridge=early_card[0],
                pregenerated_run_id=pregenerated_run_id,
                unregister_bridge=early_card[2],
                reason="file_download_failure",
            )
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
            pregenerated_run_id=pregenerated_run_id,
            early_card=early_card,
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
    agent_result = await db.execute(
        select(Agent).where(
            Agent.id == agent_id,
            Agent.deleted_at.is_(None),
        )
    )
    agent = agent_result.scalar_one_or_none()
    if not agent:
        return None, None, None

    candidates = await active_agent_model_candidates(db, agent)
    model = candidates[0] if candidates else None
    fallback_model = candidates[1] if len(candidates) > 1 else None

    return agent, model, fallback_model
