from __future__ import annotations

from pathlib import Path
from typing import Optional, List

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.skill import Skill, SkillFile

from .market_client import SuperpowersMarketClient
from .adapter import to_clawith_skill


class SkillManager:
    """Manages Superpowers skills - syncs marketplace to Clawith database."""

    def __init__(self):
        # Store marketplace in data directory
        base_dir = Path(__file__).parent / "data"
        self.client = SuperpowersMarketClient(base_dir)
        self._cache: dict[str, Skill] = {}

    async def sync_skills(self) -> int:
        """Sync all available skills from marketplace to database.

        Returns number of skills synced.
        """
        # Ensure repo is cloned
        if not self.client.is_cloned():
            success = self.client.clone()
            if not success:
                logger.error("Failed to clone marketplace, cannot sync skills")
                return 0

        # Pull latest changes
        self.client.pull_latest()

        # Get all available skills
        skill_names = self.client.list_available_skills()
        synced_count = 0

        for skill_name in skill_names:
            content = self.client.get_skill_readme(skill_name)
            if not content:
                continue

            # Convert to Clawith skill format
            skill_data = to_clawith_skill(skill_name, content)

            # Upsert into database
            synced = await self._upsert_skill(skill_data)
            if synced:
                synced_count += 1
                # Update cache
                self._cache[skill_name] = synced

        logger.info("Synced %d Superpowers skills", synced_count)
        return synced_count

    async def install_skill(self, skill_name: str) -> Optional[Skill]:
        """Install a specific skill from marketplace.

        Returns the installed Skill or None if failed.
        """
        if not self.client.is_cloned():
            success = self.client.clone()
            if not success:
                return None

        content = self.client.get_skill_readme(skill_name)
        if not content:
            logger.error("Skill %s not found in marketplace", skill_name)
            return None

        skill_data = to_clawith_skill(skill_name, content)
        skill = await self._upsert_skill(skill_data)
        if skill:
            self._cache[skill_name] = skill
            return skill

        return None

    async def update_all(self) -> int:
        """Update all installed skills to latest version.

        Returns number of updated skills.
        """
        if not self.client.is_cloned():
            return 0

        self.client.pull_latest()
        return await self.sync_skills()

    async def get_installed_skills(self) -> List[Skill]:
        """Get all installed Superpowers skills from database."""
        async with async_session() as db:
            result = await db.execute(select(Skill))
            skills = list(result.scalars().all())
            # For now, return all skills since we don't have a source field
            # In the future, we could filter by a specific category or name prefix
            return skills

    def get_skill_content(self, skill_name: str) -> Optional[str]:
        """Get the full content of a skill."""
        return self.client.get_skill_readme(skill_name)

    async def uninstall_skill(self, skill_name: str) -> bool:
        """Uninstall a skill from database. Returns True on success."""
        async with async_session() as db:
            result = await db.execute(
                select(Skill).where(Skill.name == skill_name)
            )
            skill = result.scalar_one_or_none()

            if not skill:
                return False

            await db.delete(skill)
            await db.commit()

            if skill_name in self._cache:
                del self._cache[skill_name]

            return True

    async def _upsert_skill(self, skill_data: dict) -> Optional[Skill]:
        """Upsert skill into database. Returns Skill on success."""
        async with async_session() as db:
            # Find existing skill by name
            result = await db.execute(
                select(Skill).where(Skill.name == skill_data["name"])
            )
            existing = result.scalar_one_or_none()

            if existing:
                # Update existing
                existing.description = skill_data["description"]
                await db.commit()
                await db.refresh(existing)

                # Update skill file
                file_result = await db.execute(
                    select(SkillFile).where(SkillFile.skill_id == existing.id)
                )
                skill_file = file_result.scalar_one_or_none()
                if skill_file:
                    skill_file.content = skill_data["content"]
                else:
                    skill_file = SkillFile(
                        skill_id=existing.id,
                        path="SKILL.md",
                        content=skill_data["content"]
                    )
                    db.add(skill_file)
                await db.commit()
                await db.refresh(existing)

                return existing
            else:
                # Create new
                folder_name = skill_data["name"].lower().replace(" ", "_")
                new_skill = Skill(
                    name=skill_data["name"],
                    description=skill_data["description"],
                    folder_name=folder_name,
                    is_builtin=False,
                    is_default=False
                )
                db.add(new_skill)
                await db.commit()
                await db.refresh(new_skill)

                # Create skill file
                skill_file = SkillFile(
                    skill_id=new_skill.id,
                    path="SKILL.md",
                    content=skill_data["content"]
                )
                db.add(skill_file)
                await db.commit()
                await db.refresh(new_skill)

                return new_skill
