import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import agent_tools


def _make_agent(**overrides):
    values = {
        "id": uuid.uuid4(),
        "name": "OKR Assistant",
        "role_description": "Tracks OKR progress",
        "tenant_id": uuid.uuid4(),
        "creator_id": uuid.uuid4(),
        "access_mode": "company",
        "status": "running",
        "is_expired": False,
        "expires_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _make_member(**overrides):
    values = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "user_id": None,
        "name": "张三",
        "title": "产品经理",
        "department_id": None,
        "department_path": "",
        "status": "active",
        "provider_id": None,
        "open_id": None,
        "external_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_query_roster_tool_is_available_to_agents():
    tool_names = {tool["function"]["name"] for tool in agent_tools.AGENT_TOOLS}

    assert "query_roster" in tool_names
    assert "query_roster" in agent_tools._ALWAYS_INCLUDE_CORE


@pytest.mark.asyncio
async def test_query_roster_rejects_invalid_member_type_before_db():
    result = json.loads(await agent_tools._query_roster(uuid.uuid4(), {"member_type": "team"}))

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_member_type"


def test_format_roster_agent_returns_stable_id_and_contact_tool():
    tenant_id = uuid.uuid4()
    source = _make_agent(tenant_id=tenant_id)
    target = _make_agent(tenant_id=tenant_id, access_mode="custom")

    payload = agent_tools._format_roster_agent(source, target)

    assert payload["member_type"] == "agent"
    assert payload["target_agent_id"] == str(target.id)
    assert payload["display_name"] == target.name
    assert payload["can_contact"] is True
    assert payload["contact_tools"] == ["send_message_to_agent"]


def test_format_roster_agent_marks_stopped_agent_uncontactable():
    tenant_id = uuid.uuid4()
    source = _make_agent(tenant_id=tenant_id)
    target = _make_agent(tenant_id=tenant_id, status="stopped")

    payload = agent_tools._format_roster_agent(source, target)

    assert payload["can_contact"] is False
    assert payload["contact_tools"] == []
    assert payload["unavailable_reason"] == "agent_stopped"


def test_format_roster_human_prefers_platform_then_feishu_tools():
    tenant_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    source = _make_agent(tenant_id=tenant_id)
    member = _make_member(
        tenant_id=tenant_id,
        user_id=uuid.uuid4(),
        provider_id=provider_id,
        external_id="user_xxx",
    )
    provider = SimpleNamespace(id=provider_id, provider_type="feishu")
    department = SimpleNamespace(name="产品部")

    payload = agent_tools._format_roster_human(source, member, provider, department)

    assert payload["member_type"] == "human"
    assert payload["target_member_id"] == str(member.id)
    assert payload["platform_user_id"] == str(member.user_id)
    assert payload["department"]["name"] == "产品部"
    assert payload["contact_tools"] == ["send_platform_message", "send_feishu_message"]
    assert payload["provider"]["provider_type"] == "feishu"
    assert payload["provider"]["external_id"] == "user_xxx"


def test_format_roster_human_without_contact_target_is_uncontactable():
    tenant_id = uuid.uuid4()
    source = _make_agent(tenant_id=tenant_id)
    member = _make_member(tenant_id=tenant_id)

    payload = agent_tools._format_roster_human(source, member, None, None)

    assert payload["can_contact"] is False
    assert payload["contact_tools"] == []
    assert payload["unavailable_reason"] == "missing_contact_target"


def test_roster_sort_prefers_contactable_exact_agent_match():
    members = [
        {"member_type": "human", "display_name": "OKR", "can_contact": True, "target_member_id": "h"},
        {"member_type": "agent", "display_name": "OKR", "can_contact": True, "target_agent_id": "a"},
        {"member_type": "agent", "display_name": "OKR Helper", "can_contact": True, "target_agent_id": "b"},
        {"member_type": "agent", "display_name": "OKR", "can_contact": False, "target_agent_id": "c"},
    ]

    sorted_members = sorted(members, key=lambda member: agent_tools._roster_sort_key(member, "OKR"))

    assert sorted_members[0]["target_agent_id"] == "a"
    assert sorted_members[-1]["target_agent_id"] == "c"
