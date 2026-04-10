from __future__ import annotations

import asyncio
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.security import get_current_admin
from app.models.user import User

from .skill_manager import SkillManager

router = APIRouter()


# Response models
class SuccessResponse(BaseModel):
    success: bool


class UpdateResponse(BaseModel):
    success: bool
    updated_count: int


class SkillResponse(BaseModel):
    id: str
    name: str
    description: str
    category: str
    icon: str
    folder_name: str


class InstallSkillResponse(BaseModel):
    success: bool
    skill: SkillResponse


@router.get("/available", response_model=List[str])
async def list_available_skills(
    current_user: User = Depends(get_current_admin),
):
    """List all available skills from the Superpowers Marketplace."""
    manager = SkillManager()
    if not manager.client.is_cloned():
        await asyncio.to_thread(manager.client.clone)

    return await asyncio.to_thread(manager.client.list_available_skills)


@router.get("/installed", response_model=List[SkillResponse])
async def list_installed_skills(
    current_user: User = Depends(get_current_admin),
):
    """List all currently installed Superpowers skills."""
    manager = SkillManager()
    skills = await asyncio.to_thread(manager.get_installed_skills)
    return [
        SkillResponse(
            id=str(s.id),
            name=s.name,
            description=s.description,
            category=s.category,
            icon=s.icon,
            folder_name=s.folder_name,
        )
        for s in skills
    ]


@router.post("/install/{skill_name}", response_model=InstallSkillResponse)
async def install_skill(
    skill_name: str,
    current_user: User = Depends(get_current_admin),
):
    """Install a skill from the Superpowers Marketplace."""
    manager = SkillManager()
    skill = await asyncio.to_thread(manager.install_skill, skill_name)
    if not skill:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill {skill_name} not found or installation failed",
        )

    return InstallSkillResponse(
        success=True,
        skill=SkillResponse(
            id=str(skill.id),
            name=skill.name,
            description=skill.description,
            category=skill.category,
            icon=skill.icon,
            folder_name=skill.folder_name,
        ),
    )


@router.post("/update", response_model=UpdateResponse)
async def update_all_skills(
    current_user: User = Depends(get_current_admin),
):
    """Update all installed skills to latest version."""
    manager = SkillManager()
    updated = await asyncio.to_thread(manager.update_all)
    return UpdateResponse(success=True, updated_count=updated)


@router.delete("/uninstall/{skill_name}", response_model=SuccessResponse)
async def uninstall_skill(
    skill_name: str,
    current_user: User = Depends(get_current_admin),
):
    """Uninstall a skill from the database."""
    manager = SkillManager()
    success = await asyncio.to_thread(manager.uninstall_skill, skill_name)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill {skill_name} not found",
        )

    return SuccessResponse(success=True)
