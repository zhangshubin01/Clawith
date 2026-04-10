from __future__ import annotations

from pathlib import Path
from typing import Optional, List

from loguru import logger
from sqlalchemy import select

from app.models.skill import Skill
from app.core.database import get_db

from .market_client import SuperpowersMarketClient
from .adapter import to_clawith_skill


class SkillManager:
    """Manages Superpowers skills - syncs marketplace to Clawith database."""

    def __init__(self):
        # Store marketplace in data directory
        base_dir = Path(__file__).parent / "data"
        self.client = SuperpowersMarketClient(base_dir)
        self._cache: dict[str, Skill] = {}

    def sync_skills(self) -> int:
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
            synced = self._upsert_skill(skill_data)
            if synced:
                synced_count += 1
                # Update cache
                self._cache[skill_name] = synced

        logger.info("Synced %d Superpowers skills", synced_count)
        return synced_count

    def install_skill(self, skill_name: str) -> Optional[Skill]:
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
        skill = self._upsert_skill(skill_data)
        if skill:
            self._cache[skill_name] = skill
            return skill

        return None

    def update_all(self) -> int:
        """Update all installed skills to latest version.

        Returns number of updated skills.
        """
        if not self.client.is_cloned():
            return 0

        self.client.pull_latest()
        return self.sync_skills()

    def get_installed_skills(self) -> List[Skill]:
        """Get all installed Superpowers skills from database."""
        db = next(get_db())
        skills = db.query(Skill).filter(Skill.source == "superpowers").all()
        return list(skills)

    def get_skill_content(self, skill_name: str) -> Optional[str]:
        """Get the full content of a skill."""
        return self.client.get_skill_readme(skill_name)

    def uninstall_skill(self, skill_name: str) -> bool:
        """Uninstall a skill from database. Returns True on success."""
        db = next(get_db())
        result = db.execute(
            select(Skill).where(Skill.name == skill_name, Skill.source == "superpowers")
        )
        skill = result.scalar_one_or_none()

        if not skill:
            return False

        db.delete(skill)
        db.commit()

        if skill_name in self._cache:
            del self._cache[skill_name]

        return True

    def _upsert_skill(self, skill_data: dict) -> Optional[Skill]:
        """Upsert skill into database. Returns Skill on success."""
        db = next(get_db())

        # Find existing skill by name and source
        existing = db.query(Skill).filter(
            Skill.name == skill_data["name"],
            Skill.source == "superpowers"
        ).first()

        if existing:
            # Update existing
            for key, value in skill_data.items():
                setattr(existing, key, value)
            db.commit()
            db.refresh(existing)
            return existing
        else:
            # Create new
            from app.schemas.skill import SkillCreate
            skill_create = SkillCreate(**skill_data)
            new_skill = Skill(**skill_create.model_dump())
            new_skill.source = "superpowers"
            db.add(new_skill)
            db.commit()
            db.refresh(new_skill)
            return new_skill
