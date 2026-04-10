import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from app.main import app
from app.models.user import User


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_admin_user():
    user = MagicMock(spec=User)
    user.role = "platform_admin"
    return user


@pytest.fixture
def mock_regular_user():
    user = MagicMock(spec=User)
    user.role = "member"
    return user


@patch("app.plugins.clawith_superpowers.routes.get_current_admin")
@patch("app.plugins.clawith_superpowers.routes.SkillManager")
def test_list_available_skills(mock_manager_class, mock_get_current_admin, client, mock_admin_user):
    mock_get_current_admin.return_value = mock_admin_user
    mock_manager = MagicMock()
    mock_manager.client.is_cloned.return_value = True
    mock_manager.client.list_available_skills.return_value = ["skill1", "skill2"]
    mock_manager_class.return_value = mock_manager

    response = client.get("/api/plugins/clawith-superpowers/available")

    assert response.status_code == 200
    assert response.json() == ["skill1", "skill2"]


@patch("app.plugins.clawith_superpowers.routes.get_current_admin")
@patch("app.plugins.clawith_superpowers.routes.SkillManager")
def test_list_installed_skills(mock_manager_class, mock_get_current_admin, client, mock_admin_user):
    mock_get_current_admin.return_value = mock_admin_user
    mock_manager = MagicMock()

    mock_skill = MagicMock()
    mock_skill.id = "test-id"
    mock_skill.name = "test-skill"
    mock_skill.description = "Test description"
    mock_skill.category = "test"
    mock_skill.icon = "📋"
    mock_skill.folder_name = "test-skill"
    mock_skill.is_builtin = False
    mock_skill.is_default = False
    mock_skill.created_at = None

    mock_manager.get_installed_skills.return_value = [mock_skill]
    mock_manager_class.return_value = mock_manager

    response = client.get("/api/plugins/clawith-superpowers/installed")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["id"] == "test-id"
    assert data[0]["name"] == "test-skill"


@patch("app.plugins.clawith_superpowers.routes.get_current_admin")
@patch("app.plugins.clawith_superpowers.routes.SkillManager")
def test_install_skill(mock_manager_class, mock_get_current_admin, client, mock_admin_user):
    mock_get_current_admin.return_value = mock_admin_user
    mock_manager = MagicMock()

    mock_skill = MagicMock()
    mock_skill.id = "test-id"
    mock_skill.name = "test-skill"
    mock_skill.description = "Test description"
    mock_skill.category = "test"
    mock_skill.icon = "📋"
    mock_skill.folder_name = "test-skill"

    mock_manager.install_skill.return_value = mock_skill
    mock_manager_class.return_value = mock_manager

    response = client.post("/api/plugins/clawith-superpowers/install/test-skill")

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["skill"]["name"] == "test-skill"
    mock_manager.install_skill.assert_called_once_with("test-skill")


@patch("app.plugins.clawith_superpowers.routes.get_current_admin")
@patch("app.plugins.clawith_superpowers.routes.SkillManager")
def test_install_skill_not_found(mock_manager_class, mock_get_current_admin, client, mock_admin_user):
    mock_get_current_admin.return_value = mock_admin_user
    mock_manager = MagicMock()
    mock_manager.install_skill.return_value = None
    mock_manager_class.return_value = mock_manager

    response = client.post("/api/plugins/clawith-superpowers/install/nonexistent-skill")

    assert response.status_code == 404


@patch("app.plugins.clawith_superpowers.routes.get_current_admin")
@patch("app.plugins.clawith_superpowers.routes.SkillManager")
def test_update_all_skills(mock_manager_class, mock_get_current_admin, client, mock_admin_user):
    mock_get_current_admin.return_value = mock_admin_user
    mock_manager = MagicMock()
    mock_manager.update_all.return_value = 5
    mock_manager_class.return_value = mock_manager

    response = client.post("/api/plugins/clawith-superpowers/update")

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["updated_count"] == 5


@patch("app.plugins.clawith_superpowers.routes.get_current_admin")
@patch("app.plugins.clawith_superpowers.routes.SkillManager")
def test_uninstall_skill(mock_manager_class, mock_get_current_admin, client, mock_admin_user):
    mock_get_current_admin.return_value = mock_admin_user
    mock_manager = MagicMock()
    mock_manager.uninstall_skill.return_value = True
    mock_manager_class.return_value = mock_manager

    response = client.delete("/api/plugins/clawith-superpowers/uninstall/test-skill")

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    mock_manager.uninstall_skill.assert_called_once_with("test-skill")


@patch("app.plugins.clawith_superpowers.routes.get_current_admin")
@patch("app.plugins.clawith_superpowers.routes.SkillManager")
def test_uninstall_skill_not_found(mock_manager_class, mock_get_current_admin, client, mock_admin_user):
    mock_get_current_admin.return_value = mock_admin_user
    mock_manager = MagicMock()
    mock_manager.uninstall_skill.return_value = False
    mock_manager_class.return_value = mock_manager

    response = client.delete("/api/plugins/clawith-superpowers/uninstall/nonexistent-skill")

    assert response.status_code == 404
