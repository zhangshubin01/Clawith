# backend/app/plugins/clawith_mcp/router.py
"""组合 MCP 和 OpenAI 兼容路由器。"""
from fastapi import APIRouter

from app.plugins.clawith_mcp.mcp_endpoint import router as mcp_router
from app.plugins.clawith_mcp.openai_compat import router as openai_router

router = APIRouter()
router.include_router(mcp_router)
router.include_router(openai_router)
