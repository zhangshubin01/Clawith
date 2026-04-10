from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from loguru import logger


MARKETPLACE_REPO = "https://github.com/obra/superpowers-marketplace.git"


class SuperpowersMarketClient:
    """Client for interacting with Superpowers Marketplace git repository."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.repo_dir = base_dir / "superpowers-marketplace"

    def is_cloned(self) -> bool:
        """Check if the marketplace repo is already cloned."""
        return (self.repo_dir / ".git").exists()

    def clone(self) -> bool:
        """Clone the marketplace repository. Returns True on success."""
        if self.is_cloned():
            logger.info("Marketplace already cloned at %s", self.repo_dir)
            return True

        self.base_dir.mkdir(parents=True, exist_ok=True)

        try:
            logger.info("Cloning marketplace from %s", MARKETPLACE_REPO)
            subprocess.run(
                ["git", "clone", MARKETPLACE_REPO, str(self.repo_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
            logger.info("Clone successful")
            return True
        except subprocess.CalledProcessError as e:
            logger.error("Failed to clone marketplace: %s", e.stderr)
            return False

    def pull_latest(self) -> bool:
        """Pull latest changes from marketplace. Returns True on success."""
        if not self.is_cloned():
            logger.warning("Cannot pull - repo not cloned yet")
            return False

        try:
            logger.info("Pulling latest changes from marketplace")
            subprocess.run(
                ["git", "pull"],
                cwd=self.repo_dir,
                check=True,
                capture_output=True,
                text=True,
            )
            logger.info("Pull successful")
            return True
        except subprocess.CalledProcessError as e:
            logger.error("Failed to pull latest changes: %s", e.stderr)
            return False

    def list_available_skills(self) -> list[str]:
        """List all available skills in the cloned marketplace."""
        if not self.is_cloned():
            return []

        skills_dir = self.repo_dir / "skills"
        if not skills_dir.exists():
            return []

        return sorted([
            d.name for d in skills_dir.iterdir()
            if d.is_dir() and (d / "SKILL.md").exists()
        ])

    def get_skill_path(self, skill_name: str) -> Optional[Path]:
        """Get the path to a skill directory. None if skill not found."""
        skill_name = skill_name.replace("../", "").strip()
        if not self.is_cloned():
            return None

        skill_dir = self.repo_dir / "skills" / skill_name
        if not skill_dir.exists() or not (skill_dir / "SKILL.md").exists():
            return None

        return skill_dir

    def get_skill_readme(self, skill_name: str) -> Optional[str]:
        """Get the full content of SKILL.md for a skill."""
        skill_name = skill_name.replace("../", "").strip()
        skill_path = self.get_skill_path(skill_name)
        if not skill_path:
            return None

        skill_file = skill_path / "SKILL.md"
        if not skill_file.exists():
            return None

        try:
            return skill_file.read_text(encoding="utf-8")
        except (IOError, OSError):
            return None
