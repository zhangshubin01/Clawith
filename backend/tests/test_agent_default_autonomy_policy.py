"""Default autonomy policy regression tests.

Product decision (2026-08-28): every agent created by default gets full L1
autonomy (auto-execute + log) for ALL actions. This file locks in:
  1. DEFAULT_AUTONOMY_POLICY covers the full action set at L1.
  2. The Agent.autonomy_policy column default derives from that constant via
     a fresh copy per row (no shared mutable dict).
  3. The runtime fallback in autonomy_service.check_and_enforce resolves
     missing keys to L1 (aligned with the Settings UI display).
  4. Builtin templates and folder templates (agent_templates/*/meta.yaml)
     all default to the 6-key all-L1 policy.
"""

import uuid

import pytest
from sqlalchemy import ColumnDefault

from app.models.agent import DEFAULT_AUTONOMY_POLICY, Agent
from app.services import autonomy_service as autonomy_module
from app.services.template_seeder import DEFAULT_TEMPLATES, _load_folder_templates

# Every action the runtime can gate — keep in sync with _TOOL_AUTONOMY_MAP /
# tool_step_service gates and the Settings UI action list.
ALL_ACTIONS = {
    "read_files",
    "write_workspace_files",
    "delete_files",
    "send_feishu_message",
    "send_external_message",
    "modify_soul",
    "access_business_system_read",
    "access_business_system_write",
    "create_calendar_event",
    "financial_operations",
    "web_search",
    "manage_tasks",
    "send_message_to_agent",
    "send_file_to_agent",
    "execute_code",
}

TEMPLATE_KEYS = {
    "read_files",
    "write_workspace_files",
    "delete_files",
    "send_feishu_message",
    "web_search",
    "manage_tasks",
}


def test_default_autonomy_policy_covers_all_actions_at_l1() -> None:
    assert set(DEFAULT_AUTONOMY_POLICY) == ALL_ACTIONS
    assert all(level == "L1" for level in DEFAULT_AUTONOMY_POLICY.values())


def test_agent_column_default_is_fresh_copy_of_constant() -> None:
    column = Agent.__table__.c.autonomy_policy
    assert column.default is not None
    assert isinstance(column.default, ColumnDefault)
    arg = column.default.arg
    assert callable(arg)
    # SQLAlchemy wraps callable defaults so they take a context argument.
    first = arg(None)
    second = arg(None)
    assert first == DEFAULT_AUTONOMY_POLICY
    assert all(level == "L1" for level in first.values())
    # Each row must get its own dict — mutating one must not leak to others.
    assert first is not second
    first["read_files"] = "L3"
    assert second["read_files"] == "L1"


def test_new_agent_defaults_to_all_l1_policy() -> None:
    agent = Agent(name="Default L1 Agent", creator_id=uuid.uuid4())
    # The column default applies at INSERT; before flush the attribute is
    # unset, so assert the effective policy via the column default contract.
    assert agent.autonomy_policy is None
    assert agent.__table__.c.autonomy_policy.default.arg(None) == DEFAULT_AUTONOMY_POLICY


class _ScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _DB:
    def __init__(self) -> None:
        self.added = []

    async def execute(self, _statement):
        return _ScalarResult(None)

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_check_and_enforce_missing_key_falls_back_to_l1(monkeypatch) -> None:
    for policy in ({}, None):
        agent = Agent(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            creator_id=uuid.uuid4(),
            name="Fallback Agent",
            status="idle",
            is_expired=False,
            access_mode="company",
            autonomy_policy=policy,
        )
        db = _DB()
        notified = []

        async def notify(_self, _db, _agent, _action_type, _details):
            notified.append(_action_type)

        monkeypatch.setattr(
            autonomy_module.AutonomyService,
            "_notify_creator",
            notify,
        )
        result = await autonomy_module.AutonomyService().check_and_enforce(
            db,
            agent,
            "delete_files",
            {"tool": "delete_file"},  # type: ignore[arg-type]
        )
        assert result == {
            "allowed": True,
            "level": "L1",
            "message": "Auto-executed",
        }
        assert notified == []
        # L1 only logs the audit entry — no ApprovalRequest was created.
        assert not any(isinstance(x, autonomy_module.ApprovalRequest) for x in db.added)


@pytest.mark.asyncio
async def test_check_and_enforce_explicit_l2_policy_still_notifies(monkeypatch) -> None:
    """Explicitly stored policies keep working — only the fallback changed."""
    agent = Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        name="L2 Agent",
        status="idle",
        is_expired=False,
        access_mode="company",
        autonomy_policy={"delete_files": "L2"},
    )
    db = _DB()
    notified = []

    async def notify(_self, _db, _agent, action_type, _details):
        notified.append(action_type)

    monkeypatch.setattr(
        autonomy_module.AutonomyService,
        "_notify_creator",
        notify,
    )
    result = await autonomy_module.AutonomyService().check_and_enforce(
        db,
        agent,
        "delete_files",
        {"tool": "delete_file"},  # type: ignore[arg-type]
    )
    assert result["allowed"] is True
    assert result["level"] == "L2"
    assert notified == ["delete_files"]


def test_builtin_templates_default_to_all_l1() -> None:
    assert len(DEFAULT_TEMPLATES) == 4
    for template in DEFAULT_TEMPLATES:
        policy = template["default_autonomy_policy"]
        assert set(policy) == TEMPLATE_KEYS, template["name"]
        assert all(level == "L1" for level in policy.values()), template["name"]


def test_folder_templates_default_to_all_l1() -> None:
    folder_templates = _load_folder_templates()
    assert len(folder_templates) >= 20
    for template in folder_templates:
        policy = template["default_autonomy_policy"]
        assert set(policy) == TEMPLATE_KEYS, template["name"]
        assert all(level == "L1" for level in policy.values()), template["name"]
