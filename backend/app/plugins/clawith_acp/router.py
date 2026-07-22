"""ACP WebSocket 端点 — JWT 认证 + Origin 安全检查。

安全措施:
- JWT Bearer token (header) 或 API Key (query param)
- Origin 白名单 (仅允许 IDE 来源, 防止 CSWSH)
- 复用已有 security.py 的 verify_api_key_or_token
"""

from fastapi import APIRouter, WebSocket
from loguru import logger

from app.core.security import verify_api_key_or_token
from app.plugins.clawith_acp.acp_handler import AcpHandler

router = APIRouter(tags=["acp"])


@router.websocket("/ws/acp")
async def acp_endpoint(websocket: WebSocket):
    """ACP Agent Client Protocol 端点。

    认证: JWT Bearer token (header) 或 API Key (query param).
    Security: Origin 检查防止跨站 WebSocket 劫持 (CSWSH).
    """
    # Origin 白名单检查 (IDE 插件 Ktor 客户端不发送 Origin 头 → 空字符串)
    origin = websocket.headers.get("origin", "")
    allowed_origins = {"", "null", "http://localhost", "http://127.0.0.1"}
    if origin not in allowed_origins:
        logger.warning(f"[ACP] 连接被拒绝: 非法 Origin={origin}")
        await websocket.close(code=4003, reason="Forbidden: invalid origin")
        return

    # 解析认证: 优先 header, 其次 query
    token = (
        websocket.headers.get("authorization", "").removeprefix("Bearer ").strip()
        or websocket.query_params.get("token", "")
    )
    if not token:
        logger.warning("[ACP] 连接被拒绝: 无 token")
        await websocket.close(code=4001, reason="Unauthorized: no token")
        return

    # 验证 (复用已有 security.py)
    try:
        user_id = await verify_api_key_or_token(token)
    except Exception as e:
        logger.warning(f"[ACP] 连接被拒绝: token 无效 - {e}")
        await websocket.close(code=4001, reason="Unauthorized: invalid token")
        return

    await websocket.accept()
    handler = AcpHandler(websocket, str(user_id))
    logger.info(f"[ACP] 连接成功: user_id={user_id} conn={handler.conn_id}")
    try:
        await handler.run()
    except Exception as e:
        logger.error(f"[ACP] 连接异常: {e}")
    finally:
        await handler.cleanup()
