import pytest
from unittest.mock import MagicMock, patch
from app.plugins.clawith_superpowers.skill_manager import SkillManager


@patch("app.plugins.clawith_superpowers.skill_manager.SuperpowersMarketClient")
def test_skill_manager_initialization(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    manager = SkillManager()
    assert manager.client is not None


@patch("app.plugins.clawith_superpowers.skill_manager.SuperpowersMarketClient")
@pytest.mark.asyncio
async def test_sync_skills(mock_client_class):
    # Create mock client
    mock_client = MagicMock()
    mock_client.is_cloned.return_value = True
    mock_client.pull_latest.return_value = None
    mock_client.list_available_skills.return_value = ["test-skill"]
    mock_client.get_skill_readme.return_value = "Test skill content"
    mock_client_class.return_value = mock_client

    manager = SkillManager()
    with patch.object(manager, '_upsert_skill') as mock_upsert:
        mock_upsert.return_value = True
        result = await manager.sync_skills()
        assert result == 1
        mock_client.pull_latest.assert_called_once()
        mock_client.list_available_skills.assert_called_once()
        mock_client.get_skill_readme.assert_called_once_with("test-skill")
        mock_upsert.assert_called_once()


@patch("app.plugins.clawith_superpowers.skill_manager.SuperpowersMarketClient")
@pytest.mark.asyncio
async def test_install_skill(mock_client_class):
    # Create mock client
    mock_client = MagicMock()
    mock_client.is_cloned.return_value = True
    mock_client.get_skill_readme.return_value = "Test skill content"
    mock_client_class.return_value = mock_client

    manager = SkillManager()
    with patch.object(manager, '_upsert_skill') as mock_upsert:
        mock_upsert.return_value = MagicMock(name="test-skill")
        result = await manager.install_skill("test-skill")
        assert result is not None
        assert result.name == "test-skill"
        assert "test-skill" in manager._cache


@patch("app.plugins.clawith_superpowers.skill_manager.SuperpowersMarketClient")
@pytest.mark.asyncio
async def test_uninstall_skill(mock_client_class):
    # Create mock client
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    manager = SkillManager()
    # Add to cache first
    manager._cache["test-skill"] = MagicMock(name="test-skill")

    with patch.object(manager, '_upsert_skill'):
        result = await manager.uninstall_skill("test-skill")
        assert result is False  # Should return False since we didn't actually add to DB


@patch("app.plugins.clawith_superpowers.skill_manager.SuperpowersMarketClient")
@pytest.mark.asyncio
async def test_update_all(mock_client_class):
    # Create mock client
    mock_client = MagicMock()
    mock_client.is_cloned.return_value = True
    mock_client.pull_latest.return_value = None
    mock_client.list_available_skills.return_value = []
    mock_client_class.return_value = mock_client

    manager = SkillManager()
    with patch.object(manager, 'sync_skills') as mock_sync:
        mock_sync.return_value = 0
        result = await manager.update_all()
        assert result == 0
        mock_client.pull_latest.assert_called_once()
        mock_sync.assert_called_once()
