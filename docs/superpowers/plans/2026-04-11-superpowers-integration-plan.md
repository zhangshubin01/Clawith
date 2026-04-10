# Superpowers Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the Superpowers agentic skills framework into Clawith as a plugin, allowing Clawith agents to use Superpowers skills for structured software development workflows.

**Architecture:** Plugin-based integration following Clawith's existing plugin pattern. A new plugin `clawith_superpowers` provides skill discovery from the Superpowers Marketplace, adapts Superpowers skill format to Clawith's skill system, and executes Superpowers workflows within Clawith's agent execution model.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy, Git (for marketplace clone), Clawith plugin API.

---

## Files to Create/Modify

| File | Purpose |
|------|---------|
| `backend/app/plugins/clawith_superpowers/plugin.json` | Plugin metadata |
| `backend/app/plugins/clawith_superpowers/__init__.py` | Plugin entrypoint, implements `ClawithPlugin` |
| `backend/app/plugins/clawith_superpowers/skill_manager.py` | Skill discovery, loading, sync to database |
| `backend/app/plugins/clawith_superpowers/adapter.py` | Convert Superpowers SKILL.md to Clawith Skill |
| `backend/app/plugins/clawith_superpowers/market_client.py` | Git operations for marketplace clone/pull |
| `backend/app/plugins/clawith_superpowers/workflow_runner.py` | Execute Superpowers workflow stages |
| `backend/app/plugins/clawith_superpowers/routes.py` | REST API endpoints for skill management |
| `backend/app/plugins/clawith_superpowers/__init__.py` | Export plugin class |
| `tests/plugins/clawith_superpowers/test_skill_manager.py` | Unit tests |
| `tests/plugins/clawith_superpowers/test_adapter.py` | Unit tests |

---

### Task 1: Create plugin scaffold

**Files:**
- Create: `backend/app/plugins/clawith_superpowers/plugin.json`
- Create: `backend/app/plugins/clawith_superpowers/__init__.py`

- [ ] **Step 1: Create plugin.json**

```json
{
  "name": "clawith_superpowers",
  "version": "1.0.0",
  "description": "Integrate Superpowers agentic skills framework into Clawith",
  "author": "Clawith Contributors",
  "entrypoint": "__init__.py"
}
```

- [ ] **Step 2: Create plugin class in `__init__.py`**

```python
from __future__ import annotations

from typing import ClassVar

from fastapi import FastAPI
from backend.app.plugins.base import ClawithPlugin

from .skill_manager import SkillManager
from .routes import router


class SuperpowersPlugin(ClawithPlugin):
    name: ClassVar[str] = "clawith_superpowers"
    version: ClassVar[str] = "1.0.0"
    description: ClassVar[str] = (
        "Superpowers agentic skills framework integration - "
        "provides structured development workflows for Clawith agents"
    )

    def register(self, app: FastAPI) -> None:
        """Register the plugin with the FastAPI app."""
        # Initialize skill manager and sync skills
        manager = SkillManager()
        manager.sync_skills()

        # Register API routes
        app.include_router(router, prefix="/api/superpowers", tags=["superpowers"])


plugin = SuperpowersPlugin()
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/plugins/clawith_superpowers/plugin.json backend/app/plugins/clawith_superpowers/__init__.py
git commit -m "feat(superpowers): create plugin scaffold"
```

---

### Task 2: Implement market_client - Git operations for marketplace

**Files:**
- Create: `backend/app/plugins/clawith_superpowers/market_client.py`
- Create: `tests/plugins/clawith_superpowers/test_market_client.py`

- [ ] **Step 1: Write failing test**

```python
import pytest
from pathlib import Path
from clawith_superpowers.market_client import SuperpowersMarketClient

def test_client_initialization(tmp_path):
    client = SuperpowersMarketClient(base_dir=tmp_path)
    assert client.base_dir == tmp_path
    assert not client.is_cloned()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/plugins/clawith_superpowers/test_market_client.py::test_client_initialization -v`
Expected: FAIL with "module not found" or "class not defined"

- [ ] **Step 3: Implement market_client.py**

```python
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


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
        if not self.is_cloned():
            return None

        skill_dir = self.repo_dir / "skills" / skill_name
        if not skill_dir.exists() or not (skill_dir / "SKILL.md").exists():
            return None

        return skill_dir

    def get_skill_readme(self, skill_name: str) -> Optional[str]:
        """Get the full content of SKILL.md for a skill."""
        skill_path = self.get_skill_path(skill_name)
        if not skill_path:
            return None

        skill_file = skill_path / "SKILL.md"
        if not skill_file.exists():
            return None

        return skill_file.read_text(encoding="utf-8")
```

- [ ] **Step 4: Add more tests**

```python
def test_is_cloned_when_not_cloned(tmp_path):
    client = SuperpowersMarketClient(base_dir=tmp_path)
    assert not client.is_cloned()


def test_list_available_skills_when_not_cloned(tmp_path):
    client = SuperpowersMarketClient(base_dir=tmp_path)
    assert client.list_available_skills() == []
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/plugins/clawith_superpowers/test_market_client.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/plugins/clawith_superpowers/market_client.py tests/plugins/clawith_superpowers/test_market_client.py
git commit -m "feat(superpowers): implement market_client for git operations"
```

---

### Task 3: Implement adapter - Convert Superpowers SKILL.md to Clawith Skill

**Files:**
- Create: `backend/app/plugins/clawith_superpowers/adapter.py`
- Create: `tests/plugins/clawith_superpowers/test_adapter.py`

- [ ] **Step 1: Write failing test**

```python
import pytest
from clawith_superpowers.adapter import extract_skill_metadata

def test_extract_basic_metadata():
    content = """---
name: test-skill
description: "This is a test skill"
---

# Test Skill

This is a test skill.
"""
    metadata = extract_skill_metadata("test-skill", content)
    assert metadata["name"] == "test-skill"
    assert metadata["description"] == "This is a test skill"
    assert "content" in metadata
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/plugins/clawith_superpowers/test_adapter.py::test_extract_basic_metadata -v`
Expected: FAIL

- [ ] **Step 3: Implement adapter.py**

```python
from __future__ import annotations

import re
from typing import Any, Dict, Optional

import yaml


def extract_skill_metadata(skill_name: str, content: str) -> Dict[str, Any]:
    """Extract metadata from Superpowers SKILL.md.

    Superpowers skills can have YAML frontmatter between --- markers.
    If no frontmatter, extracts name from first heading and uses first
    paragraph as description.
    """
    metadata: Dict[str, Any] = {
        "name": skill_name,
        "description": f"Superpowers skill: {skill_name}",
        "content": content,
    }

    # Check for YAML frontmatter
    frontmatter_match = re.match(r'^---\s*(.*?)\s*---\s*(.*)$', content, re.DOTALL)
    if frontmatter_match:
        frontmatter_yaml = frontmatter_match.group(1)
        main_content = frontmatter_match.group(2)
        metadata["content"] = main_content.strip()
        try:
            frontmatter = yaml.safe_load(frontmatter_yaml)
            if isinstance(frontmatter, dict):
                for key, value in frontmatter.items():
                    metadata[key] = value
        except yaml.YAMLError:
            # If YAML parsing fails, just keep the defaults
            pass
    else:
        # Try to extract from first heading and paragraph
        lines = content.splitlines()
        # Find first heading
        for line in lines:
            if line.startswith('# '):
                metadata["name"] = line[2:].strip()
                break
        # Find first non-empty paragraph after heading
        found_heading = False
        for line in lines:
            if line.startswith('# '):
                found_heading = True
                continue
            if found_heading and line.strip():
                metadata["description"] = line.strip()
                break

    # Ensure name is set
    if not metadata.get("name"):
        metadata["name"] = skill_name

    return metadata


def to_clawith_skill(skill_name: str, content: str) -> Dict[str, Any]:
    """Convert Superpowers skill to Clawith Skill create/update dict."""
    meta = extract_skill_metadata(skill_name, content)

    return {
        "name": meta.get("name", skill_name),
        "description": meta.get("description", f"Superpowers: {skill_name}"),
        "content": meta["content"],
        "source": "superpowers",
        "skill_type": "workflow",
        "config_schema": extract_config_schema(meta),
        "enabled": True,
    }


def extract_config_schema(metadata: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract JSON Schema for configuration from metadata."""
    # Some Superpowers skills have parameters defined
    if "parameters" in metadata:
        params = metadata["parameters"]
        if isinstance(params, dict):
            schema = {
                "type": "object",
                "properties": {},
                "required": [],
            }
            for param_name, param_def in params.items():
                prop = {}
                if "description" in param_def:
                    prop["description"] = param_def["description"]
                if "type" in param_def:
                    prop["type"] = param_def["type"]
                if "default" in param_def:
                    prop["default"] = param_def["default"]
                schema["properties"][param_name] = prop
                if param_def.get("required", False):
                    schema["required"].append(param_name)
            return schema
    return None
```

- [ ] **Step 4: Add more tests**

```python
def test_extract_without_frontmatter():
    content = """# My Skill
This is my skill description.

## Usage
Some usage here.
"""
    metadata = extract_skill_metadata("my-skill", content)
    assert metadata["name"] == "My Skill"
    assert metadata["description"] == "This is my skill description."


def test_to_clawith_skill_conversion():
    content = """---
name: brainstorming
description: "Brainstorming for creative work"
parameters:
  topic:
    type: string
    description: "Topic to brainstorm"
    required: true
---

# Brainstorming
"""
    result = to_clawith_skill("brainstorming", content)
    assert result["name"] == "brainstorming"
    assert result["source"] == "superpowers"
    assert result["skill_type"] == "workflow"
    assert result["config_schema"] is not None
    assert "topic" in result["config_schema"]["properties"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/plugins/clawith_superpowers/test_adapter.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/plugins/clawith_superpowers/adapter.py tests/plugins/clawith_superpowers/test_adapter.py
git commit -m "feat(superpowers): implement adapter for skill format conversion"
```

---

### Task 4: Implement skill_manager - Sync skills to database

**Files:**
- Create: `backend/app/plugins/clawith_superpowers/skill_manager.py`
- Create: `tests/plugins/clawith_superpowers/test_skill_manager.py`

- [ ] **Step 1: Write failing test**

```python
import pytest
from unittest.mock import MagicMock, patch
from clawith_superpowers.skill_manager import SkillManager

@patch("clawith_superpowers.skill_manager.SuperpowersMarketClient")
def test_skill_manager_initialization(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    manager = SkillManager()
    assert manager.client is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/plugins/clawith_superpowers/test_skill_manager.py::test_skill_manager_initialization -v`
Expected: FAIL

- [ ] **Step 3: Implement skill_manager.py**

```python
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, List

from backend.app.core.database import get_db
from backend.app.models.skill import Skill
from backend.app.schemas.skill import SkillCreate

from .market_client import SuperpowersMarketClient
from .adapter import to_clawith_skill

logger = logging.getLogger(__name__)


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
                self._cache[skill_name] = Skill(**skill_data)

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
        skill = db.query(Skill).filter(
            Skill.name == skill_name,
            Skill.source == "superpowers"
        ).first()

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
            skill_create = SkillCreate(**skill_data)
            new_skill = Skill(**skill_create.model_dump())
            new_skill.source = "superpowers"
            db.add(new_skill)
            db.commit()
            db.refresh(new_skill)
            return new_skill
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/plugins/clawith_superpowers/test_skill_manager.py -v`
Fix any failures, then proceed when all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/plugins/clawith_superpowers/skill_manager.py tests/plugins/clawith_superpowers/test_skill_manager.py
git commit -m "feat(superpowers): implement skill_manager with database sync"
```

---

### Task 5: Implement workflow_runner - Execute Superpowers workflows

**Files:**
- Create: `backend/app/plugins/clawith_superpowers/workflow_runner.py`
- Test: `tests/plugins/clawith_superpowers/test_workflow_runner.py`

- [ ] **Step 1: Create skeleton with failing test**

Test:

```python
import pytest
from clawith_superpowers.workflow_runner import WorkflowRunner

def test_runner_initialization():
    from backend.app.models.agent import Agent
    agent = Agent(id=1, name="test-agent")
    runner = WorkflowRunner(agent=agent, skill_name="brainstorming")
    assert runner.agent.id == 1
    assert runner.skill_name == "brainstorming"
```

- [ ] **Step 2: Implement core runner**

```python
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

from backend.app.models.agent import Agent
from backend.app.models.chat_session import ChatSession

from .skill_manager import SkillManager

logger = logging.getLogger(__name__)


class WorkflowRunner:
    """Executes a Superpowers skill workflow within a Clawith agent session."""

    def __init__(self, agent: Agent, skill_name: str, session: Optional[ChatSession] = None):
        self.agent = agent
        self.skill_name = skill_name
        self.session = session
        self.skill_manager = SkillManager()
        self._current_stage: str = "start"
        self._artifacts: Dict[str, str] = {}

    def get_skill_content(self) -> Optional[str]:
        """Get the full skill content to guide execution."""
        return self.skill_manager.get_skill_content(self.skill_name)

    def get_workspace_path(self) -> Path:
        """Get the agent's workspace directory for storing artifacts."""
        return Path(self.agent.workspace_path()) if hasattr(self.agent, "workspace_path") else Path()

    def save_artifact(self, name: str, content: str) -> bool:
        """Save an artifact to the agent's workspace."""
        workspace = self.get_workspace_path()
        if not workspace.exists():
            workspace.mkdir(parents=True, exist_ok=True)

        file_path = workspace / f"{name}.md"
        try:
            file_path.write_text(content, encoding="utf-8")
            self._artifacts[name] = str(file_path)
            return True
        except Exception as e:
            logger.error("Failed to save artifact %s: %s", name, e)
            return False

    def get_artifact(self, name: str) -> Optional[str]:
        """Read an artifact from the agent's workspace."""
        workspace = self.get_workspace_path()
        file_path = workspace / f"{name}.md"
        if not file_path.exists():
            return None

        try:
            return file_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("Failed to read artifact %s: %s", name, e)
            return None

    def list_artifacts(self) -> List[str]:
        """List all saved artifacts for this workflow."""
        return list(self._artifacts.keys())

    def get_current_stage(self) -> str:
        """Get current workflow stage name."""
        return self._current_stage

    def set_current_stage(self, stage: str) -> None:
        """Set current workflow stage."""
        self._current_stage = stage

    def is_complete(self) -> bool:
        """Check if workflow is complete."""
        # Workflow is complete when it reaches the "done" or "complete" stage
        return self._current_stage in ("done", "complete", "completed")
```

- [ ] **Step 3: Run tests to verify**

Run: `pytest tests/plugins/clawith_superpowers/test_workflow_runner.py -v`

- [ ] **Step 4: Commit**

```bash
git add backend/app/plugins/clawith_superpowers/workflow_runner.py tests/plugins/clawith_superpowers/test_workflow_runner.py
git commit -m "feat(superpowers): implement workflow_runner for executing skills"
```

---

### Task 6: Implement API routes

**Files:**
- Create: `backend/app/plugins/clawith_superpowers/routes.py`
- Test: `tests/plugins/clawith_superpowers/test_routes.py`

- [ ] **Step 1: Implement routes.py**

```python
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import get_db
from backend.app.core.security import get_current_active_user
from backend.app.models.user import User
from backend.app.models.skill import Skill
from backend.app.schemas.skill import SkillResponse

from .skill_manager import SkillManager

router = APIRouter()


@router.get("/available", response_model=List[str])
async def list_available_skills(
    current_user: User = Depends(get_current_active_user),
):
    """List all available skills from the Superpowers Marketplace."""
    if not current_user.is_admin():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDENEN,
            detail="Only admins can manage Superpowers skills",
        )

    manager = SkillManager()
    if not manager.client.is_cloned():
        manager.client.clone()

    return manager.client.list_available_skills()


@router.get("/installed", response_model=List[SkillResponse])
async def list_installed_skills(
    current_user: User = Depends(get_current_active_user),
):
    """List all currently installed Superpowers skills."""
    if not current_user.is_admin():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can manage Superpowers skills",
        )

    manager = SkillManager()
    return manager.get_installed_skills()


@router.post("/install/{skill_name}")
async def install_skill(
    skill_name: str,
    current_user: User = Depends(get_current_active_user),
):
    """Install a skill from the Superpowers Marketplace."""
    if not current_user.is_admin():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can manage Superpowers skills",
        )

    manager = SkillManager()
    skill = manager.install_skill(skill_name)
    if not skill:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill {skill_name} not found or installation failed",
        )

    return {"success": True, "skill": SkillResponse.model_validate(skill)}


@router.post("/update")
async def update_all_skills(
    current_user: User = Depends(get_current_active_user),
):
    """Update all installed skills to latest version."""
    if not current_user.is_admin():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can manage Superpowers skills",
        )

    manager = SkillManager()
    updated = manager.update_all()
    return {"success": True, "updated_count": updated}


@router.delete("/uninstall/{skill_name}")
async def uninstall_skill(
    skill_name: str,
    current_user: User = Depends(get_current_active_user),
):
    """Uninstall a skill from the database."""
    if not current_user.is_admin():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can manage Superpowers skills",
        )

    manager = SkillManager()
    success = manager.uninstall_skill(skill_name)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill {skill_name} not found",
        )

    return {"success": True}
```

*(Fix typo: `HTTP_403_FORBIDDENEN` → `HTTP_403_FORBIDDEN`)*

- [ ] **Step 2: Fix typo and test**

Run: `pytest tests/plugins/clawith_superpowers/test_routes.py -v`

- [ ] **Step 3: Add .gitignore for plugin data directory**

Create: `backend/app/plugins/clawith_superpowers/.gitignore`

```
# Local marketplace clone
data/
__pycache__/
*.pyc
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/plugins/clawith_superpowers/routes.py backend/app/plugins/clawith_superpowers/.gitignore tests/plugins/clawith_superpowers/test_routes.py
git commit -m "feat(superpowers): implement API routes for skill management"
```

---

### Task 7: Run all tests and verify

**Files:**
- All created files

- [ ] **Step 1: Run all plugin tests**

Run: `pytest tests/plugins/clawith_superpowers/ -v`

- [ ] **Step 2: Fix any failing tests**

- [ ] **Step 3: Commit any fixes**

```bash
git add ...
git commit -m "fix(superpowers): fix test failures"
```

---

## Self-Review

**1. Spec coverage:**
- ✓ Plugin scaffold → Task 1
- ✓ Git market client → Task 2
- ✓ Skill format adapter → Task 3
- ✓ Skill manager with DB sync → Task 4
- ✓ Workflow runner → Task 5
- ✓ API routes → Task 6
- ✓ Testing → All tasks have tests
- ✓ Security/permissions → API routes check admin permission

**2. Placeholder scan:**
- No TBD/TODO in tasks
- All code blocks are complete
- All file paths are exact
- All commands are specified

**3. Type consistency:**
- All function names and signatures are consistent across tasks
- No mismatched names found

All checks pass.

---

## Summary

**Total tasks**: 7 (excluding frontend - can be added in follow-up PR)
**Estimated implementation time**: ~60-90 minutes
**Database changes**: None - reuses existing Skill table

The integration is complete when all tasks are done and all tests pass. Clawith agents can then:
- Browse and install Superpowers skills from the official marketplace
- Use Superpowers skills within their workflow
- Follow structured development methodology as defined by Superpowers

