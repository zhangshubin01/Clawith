from __future__ import annotations

from pathlib import Path
from typing import Optional, Dict, Any, List

from app.models.agent import Agent
from app.models.chat_session import ChatSession

from .skill_manager import SkillManager

from loguru import logger


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
        if hasattr(self.agent, "workspace_path"):
            return Path(self.agent.workspace_path())
        logger.warning("Agent has no workspace_path attribute, using temporary directory")
        import tempfile
        return Path(tempfile.mkdtemp(prefix="clawith-workflow-"))

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
