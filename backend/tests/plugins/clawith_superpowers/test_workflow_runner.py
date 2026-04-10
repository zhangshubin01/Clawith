import pytest
from app.plugins.clawith_superpowers.workflow_runner import WorkflowRunner
from pathlib import Path
import tempfile
import os


def test_runner_initialization():
    from app.models.agent import Agent
    agent = Agent(id=1, name="test-agent")
    runner = WorkflowRunner(agent=agent, skill_name="brainstorming")
    assert runner.agent.id == 1
    assert runner.skill_name == "brainstorming"


def test_artifact_operations():
    """Test saving and retrieving artifacts."""
    from app.models.agent import Agent

    class MockAgent:
        def __init__(self, workspace_path=None):
            self.id = 1
            self.name = "test-agent"
            self._workspace_path = workspace_path

        def workspace_path(self):
            return self._workspace_path if self._workspace_path else tempfile.mkdtemp()

    agent = MockAgent()
    runner = WorkflowRunner(agent=agent, skill_name="test-skill")

    # Save artifact
    content = "Test artifact content"
    assert runner.save_artifact("test-artifact", content) is True

    # Retrieve artifact
    retrieved = runner.get_artifact("test-artifact")
    assert retrieved == content

    # List artifacts
    assert "test-artifact" in runner.list_artifacts()


def test_artifact_not_found():
    """Test getting a non-existent artifact returns None."""
    from app.models.agent import Agent
    agent = Agent(id=1, name="test-agent")
    runner = WorkflowRunner(agent=agent, skill_name="test-skill")

    assert runner.get_artifact("non-existent-artifact") is None


def test_workflow_stages():
    """Test workflow stages functionality."""
    from app.models.agent import Agent
    agent = Agent(id=1, name="test-agent")
    runner = WorkflowRunner(agent=agent, skill_name="test-skill")

    assert runner.get_current_stage() == "start"
    runner.set_current_stage("planning")
    assert runner.get_current_stage() == "planning"
    runner.set_current_stage("execution")
    assert runner.get_current_stage() == "execution"
    runner.set_current_stage("done")
    assert runner.is_complete()


def test_no_workspace_path_fallback():
    """Test that get_workspace_path() falls back to temporary directory when agent has no workspace_path method."""
    from app.models.agent import Agent

    class AgentWithoutWorkspacePath:
        def __init__(self):
            self.id = 1
            self.name = "test-agent"

    agent = AgentWithoutWorkspacePath()
    runner = WorkflowRunner(agent=agent, skill_name="test-skill")

    workspace_path = runner.get_workspace_path()
    assert isinstance(workspace_path, Path)
    assert workspace_path.exists()

    # Cleanup
    if workspace_path and workspace_path.exists() and "clawith-workflow" in str(workspace_path):
        import shutil
        try:
            shutil.rmtree(str(workspace_path))
        except Exception:
            pass
