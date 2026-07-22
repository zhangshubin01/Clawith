"""Clawith ACP 插件 — Agent Client Protocol WebSocket 端点。

使用 ACP JSON-RPC 2.0 协议，通过 WebSocket 直连 IntelliJ 插件。
遵循 Clawith 插件架构 (plugins/clawith_acp/)。

注册: ClawithAcpPlugin.register(app) → 挂载 /ws/acp + 安装工具钩子。
"""

from app.plugins.base import ClawithPlugin
from fastapi import FastAPI
from loguru import logger


class ClawithAcpPlugin(ClawithPlugin):
    name = "clawith-acp"
    version = "0.1.0"
    description = "ACP WebSocket endpoint for IntelliJ plugin"

    def register(self, app: FastAPI) -> None:
        """向 FastAPI app 注册 ACP WebSocket 路由和工具钩子。

        路由在 __init__ 中懒加载导入, 避免循环引用。
        工具钩子在注册时立即安装, 确保 IDE 工具回调可用。
        """
        from app.plugins.clawith_acp.router import router
        app.include_router(router)

        from app.plugins.clawith_acp.tool_bridge import install_acp_tool_hooks
        install_acp_tool_hooks()

        logger.info("[ACP] 插件已注册: /ws/acp")
