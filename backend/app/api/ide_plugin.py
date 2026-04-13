"""IDEA Plugin specific API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from app.database import get_db
from app.models.user import User
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.core.security import verify_api_key_or_token
from app.core.permissions import check_agent_access

router = APIRouter(prefix="/api/ide-plugin", tags=["ide-plugin"])


@router.get("/agents")
async def list_agents_for_ide(
    x_api_key: str = Header(..., description="API Key (cw-xxx) or JWT"),
    db: AsyncSession = Depends(get_db),
):
    """获取用户可访问的智能体列表 (简化版,仅返回必要字段)"""
    try:
        user_id = await verify_api_key_or_token(x_api_key)
    except HTTPException as e:
        raise e

    # 获取用户信息以进行权限检查
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # 获取所有智能体并过滤有权限的
    agent_result = await db.execute(select(Agent))
    agents = agent_result.scalars().all()
    
    accessible_agents = []
    for agent in agents:
        try:
            # 简单的权限检查：如果用户是平台管理员或智能体所有者，或者有明确的访问权限
            # 这里简化处理，实际项目中可能需要更复杂的逻辑
            if user.role == "platform_admin" or agent.owner_id == user_id:
                accessible_agents.append(agent)
            # 如果有更细粒度的权限表，应在此处查询
        except Exception:
            continue

    return [
        {
            "id": str(agent.id),
            "name": agent.name,
            "avatar_url": agent.avatar_url,
            "role_description": agent.role_description,
            "primary_model_id": str(agent.primary_model_id) if agent.primary_model_id else None
        }
        for agent in accessible_agents
    ]


@router.get("/models")
async def list_models_for_ide(
    x_api_key: str = Header(..., description="API Key (cw-xxx) or JWT"),
    db: AsyncSession = Depends(get_db),
):
    """获取可用的 LLM 模型列表"""
    try:
        user_id = await verify_api_key_or_token(x_api_key)
    except HTTPException as e:
        raise e

    # 获取所有启用的模型
    model_result = await db.execute(select(LLMModel).where(LLMModel.enabled == True))
    models = model_result.scalars().all()

    return [
        {
            "id": str(model.id),
            "provider": model.provider,
            "model": model.model,
            "label": model.label,
            "supports_vision": model.supports_vision
        }
        for model in models
    ]
