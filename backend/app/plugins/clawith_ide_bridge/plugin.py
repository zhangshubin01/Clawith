"""Clawith IDE Bridge 插件 — IDEA 插件双向桥接。

为 Clawith 后端与 IDEA 插件之间提供：
1. 双向工具调用（后端可调用 IDE 端工具，IDE 端可回传结果）
2. 上下文同步（IDE 项目路径、当前文件、Android 组件信息）
3. Diff 预览（代码变更在 IDE 端展示）
"""
from __future__ import annotations

from loguru import logger
from fastapi import FastAPI
from app.plugins.base import ClawithPlugin


class ClawithIdeBridgePlugin(ClawithPlugin):
    name = "clawith-ide-bridge"
    version = "1.0.0"
    description = "Provides bidirectional tool calling, context sync, and diff preview for IDEA plugins."

    def register(self, app: FastAPI) -> None:
        """注册 IDE Bridge WebSocket 路由。
        
        路由端点：
        - /api/ide-bridge/ws        WebSocket 双向通信
        - /api/ide-bridge/status    健康检查
        - /api/ide-bridge/agents    智能体列表查询
        """
        from app.plugins.clawith_ide_bridge.router import router
        app.include_router(router)
        logger.info("[IDE-Bridge] 插件已注册: version={}", self.version)


plugin = ClawithIdeBridgePlugin()
